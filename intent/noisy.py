"""Noisy network elements.
"""
import logging
import numpy as np
import theano
import contextlib
from theano import tensor
from theano.printing import Print
from picklable_itertools import chain, repeat, imap
from picklable_itertools.extras import partition_all

from blocks.bricks import Brick
from blocks.bricks import Feedforward
from blocks.bricks import FeedforwardSequence
from blocks.bricks import Initializable
from blocks.bricks import Linear
from blocks.bricks import MLP
from blocks.bricks import Random
from blocks.bricks import Rectifier
from blocks.bricks import Softmax
from blocks.bricks import application
from blocks.bricks import lazy
from blocks.bricks.conv import Convolutional, ConvolutionalSequence
from blocks.bricks.conv import Flattener, MaxPooling
from blocks.bricks.interfaces import RNGMixin
from blocks.extensions import SimpleExtension
from blocks.extensions.monitoring import DataStreamMonitoring
from blocks.graph import add_annotation
from blocks.initialization import Constant, Uniform, IsotropicGaussian
from blocks.monitoring.evaluators import DatasetEvaluator
from blocks.roles import add_role, AuxiliaryRole, ParameterRole
from blocks.utils import shared_floatx_zeros
from blocks.utils import find_bricks
from intent.flatten import GlobalAverageFlattener
import collections
from collections import OrderedDict
import fuel
from fuel.schemes import BatchScheme
from toolz.itertoolz import interleave


logger = logging.getLogger(__name__)

class NoiseRole(ParameterRole):
    pass

# Role for parameters that are used to inject noise during training.
NOISE = NoiseRole()


class NitsRole(AuxiliaryRole):
    pass

# Role for variables that quantify the number of nits at a unit.
NITS = NitsRole()


class LogSigmaRole(AuxiliaryRole):
    pass

# Role for parameters that are used to inject noise during training.
LOG_SIGMA = LogSigmaRole()


# Annotate all the nits variables
def copy_and_tag_noise(variable, brick, role, name):
    """Helper method to copy a variable and annotate it."""
    copy = variable.copy()
    # Theano name
    copy.name = "{}_apply_{}".format(brick.name, name)
    add_annotation(copy, brick)
    # Blocks name
    copy.tag.name = name
    add_role(copy, role)
    return copy

class UnitNoiseGenerator(Random):
    def __init__(self, std=1.0, **kwargs):
        self.std = std
        super(UnitNoiseGenerator, self).__init__(**kwargs)

    @application(inputs=['param'], outputs=['output'])
    def apply(self, param):
        return self.theano_rng.normal(param.shape, std=self.std)

class NoiseExtension(SimpleExtension, RNGMixin):
    def __init__(self, noise_parameters=None, **kwargs):
        kwargs.setdefault("before_training", True)
        kwargs.setdefault("after_training", True)
        self.noise_parameters = noise_parameters
        std = 1.0
        self.noise_init = IsotropicGaussian(std=std)
        theano_seed = self.rng.randint(np.iinfo(np.int32).max)
        self.theano_generator = UnitNoiseGenerator(
                std=std, theano_seed=theano_seed)
        self.noise_updates = OrderedDict(
            [(param, self.theano_generator.apply(param))
                for param in self.noise_parameters])
        super(NoiseExtension, self).__init__(**kwargs)

    def do(self, callback_name, *args):
        self.parse_args(callback_name, args)
        if callback_name == 'before_training':
            # Before training, intiaizlize noise
            for p in self.noise_parameters:
                self.noise_init.initialize(p, self.rng)
            # And set up update to change noise on every update
            self.main_loop.algorithm.add_updates(self.noise_updates)
        if callback_name == 'after_training':
            # After training, zero noise again.
            for p in self.noise_parameters:
                v = p.get_value()
                p.set_value(np.zeros(v.shape, dtype=v.dtype))

class NoisyLinear(Initializable, Feedforward, Random):
    """Linear transformation sent through a learned noisy channel.

    Parameters
    ----------
    input_dim : int
        The dimension of the input. Required by :meth:`~.Brick.allocate`.
    output_dim : int
        The dimension of the output. Required by :meth:`~.Brick.allocate`.
    num_pieces : int
        The number of linear functions. Required by
        :meth:`~.Brick.allocate`.
    """
    @lazy(allocation=['input_dim', 'output_dim', 'noise_batch_size'])
    def __init__(self, input_dim, output_dim, noise_batch_size,
            prior_mean=0, prior_noise_level=0, **kwargs):
        self.linear = Linear()
        self.mask = Linear(name='mask')
        children = [self.linear, self.mask]
        kwargs.setdefault('children', []).extend(children)
        super(NoisyLinear, self).__init__(**kwargs)

        self.input_dim = input_dim
        self.output_dim = output_dim
        self.noise_batch_size = noise_batch_size
        self.prior_mean = prior_mean
        self.prior_noise_level = prior_noise_level

    def _push_allocation_config(self):
        self.linear.input_dim = self.input_dim
        self.linear.output_dim = self.output_dim
        self.mask.input_dim = self.output_dim
        self.mask.output_dim = self.output_dim

    def _allocate(self):
        N = shared_floatx_zeros(
                (self.noise_batch_size, self.output_dim), name='N')
        add_role(N, NOISE)
        self.parameters.append(N)

    @application(inputs=['input_'], outputs=['output'])
    def apply(self, input_, application_call):
        """Apply the linear transformation followed by masking with noise.
        Parameters
        ----------
        input_ : :class:`~tensor.TensorVariable`
            The input on which to apply the transformations
        Returns
        -------
        output : :class:`~tensor.TensorVariable`
            The transformed input
        """
        pre_noise = self.linear.apply(input_)
        noise_level = (self.prior_noise_level
                - tensor.clip(self.mask.apply(pre_noise), -16, 16))
        noise_level = copy_and_tag_noise(
                noise_level, self, LOG_SIGMA, 'log_sigma')

        # Allow incomplete batches by just taking the noise that is needed
        # noise = Print('noise')(self.parameters[0][:noise_level.shape[0], :])
        noise = self.parameters[0][:noise_level.shape[0], :]
        # noise = Print('noise')(self.theano_rng.normal(noise_level.shape))
        kl = (
            self.prior_noise_level - noise_level 
            + 0.5 * (
                tensor.exp(2 * noise_level)
                + (pre_noise - self.prior_mean) ** 2
                ) / tensor.exp(2 * self.prior_noise_level)
            - 0.5
            )
        application_call.add_auxiliary_variable(kl, roles=[NITS], name='nits')
        return pre_noise + tensor.exp(noise_level) * noise

    def get_dim(self, name):
        if name == 'input_':
            return self.linear.get_dim(name)
        if name == 'output':
            return self.linear.get_dim(name)
        if name == 'nits':
            return self.linear.get_dim('output')
        return super(NoisyLinear, self).get_dim(name)


class NoisyConvolutional2(Initializable, Feedforward, Random):
    """Convolutional transformation sent through a learned noisy channel.

    Applies the noise after the Relu rather than before it.

    Parameters (same as Convolutional)
    """
    @lazy(allocation=[
        'filter_size', 'num_filters', 'num_channels', 'noise_batch_size'])
    def __init__(self, filter_size, num_filters, num_channels, noise_batch_size,
                 image_size=(None, None), step=(1, 1), border_mode='valid',
                 tied_biases=True,
                 prior_mean=0, prior_noise_level=0, **kwargs):
        self.convolution = Convolutional()
        self.rectifier = Rectifier()
        self.mask = Convolutional(name='mask')
        children = [self.convolution, self.rectifier, self.mask]
        kwargs.setdefault('children', []).extend(children)
        super(NoisyConvolutional2, self).__init__(**kwargs)
        self.filter_size = filter_size
        self.num_filters = num_filters
        self.num_channels = num_channels
        self.noise_batch_size = noise_batch_size
        self.image_size = image_size
        self.step = step
        self.border_mode = border_mode
        self.tied_biases = tied_biases
        self.prior_mean = prior_mean
        self.prior_noise_level = prior_noise_level

    def _push_allocation_config(self):
        self.convolution.filter_size = self.filter_size
        self.convolution.num_filters = self.num_filters
        self.convolution.num_channels = self.num_channels
        # self.convolution.batch_size = self.batch_size
        self.convolution.image_size = self.image_size
        self.convolution.step = self.step
        self.convolution.border_mode = self.border_mode
        self.convolution.tied_biases = self.tied_biases
        self.mask.filter_size = (1, 1)
        self.mask.num_filters = self.num_filters
        self.mask.num_channels = self.num_filters
        # self.mask.batch_size = self.batch_size
        self.mask.image_size = self.convolution.get_dim('output')[1:]
        # self.mask.step = self.step
        # self.mask.border_mode = self.border_mode
        self.mask.tied_biases = self.tied_biases

    def _allocate(self):
        out_shape = self.convolution.get_dim('output')
        N = shared_floatx_zeros((self.noise_batch_size,) + out_shape, name='N')
        add_role(N, NOISE)
        self.parameters.append(N)

    @application(inputs=['input_'], outputs=['output'])
    def apply(self, input_, application_call):
        """Apply the linear transformation followed by masking with noise.
        Parameters
        ----------
        input_ : :class:`~tensor.TensorVariable`
            The input on which to apply the transformations
        Returns
        -------
        output : :class:`~tensor.TensorVariable`
            The transformed input
        """
        from theano.printing import Print

        pre_noise = self.rectifier.apply(self.convolution.apply(input_))
        # noise_level = self.mask.apply(input_)
        noise_level = (self.prior_noise_level
                - tensor.clip(self.mask.apply(pre_noise), -16, 16))
        noise_level = copy_and_tag_noise(
                noise_level, self, LOG_SIGMA, 'log_sigma')
        # Allow incomplete batches by just taking the noise that is needed
        noise = self.parameters[0][:noise_level.shape[0], :, :, :]
        # noise = self.theano_rng.normal(noise_level.shape)
        kl = (
            self.prior_noise_level - noise_level 
            + 0.5 * (
                tensor.exp(2 * noise_level)
                + (pre_noise - self.prior_mean) ** 2
                ) / tensor.exp(2 * self.prior_noise_level)
            - 0.5
            )
        application_call.add_auxiliary_variable(kl, roles=[NITS], name='nits')
        return pre_noise + tensor.exp(noise_level) * noise

    def get_dim(self, name):
        if name == 'input_':
            return self.convolution.get_dim(name)
        if name == 'output':
            return self.convolution.get_dim(name)
        if name == 'nits':
            return self.convolution.get_dim('output')
        return super(NoisyConvolutional2, self).get_dim(name)

    @property
    def num_output_channels(self):
        return self.num_filters


class NoisyConvolutional(Initializable, Feedforward, Random):
    """Convolutional transformation sent through a learned noisy channel.

    Parameters (same as Convolutional)
    """
    @lazy(allocation=[
        'filter_size', 'num_filters', 'num_channels', 'noise_batch_size'])
    def __init__(self, filter_size, num_filters, num_channels, noise_batch_size,
                 image_size=(None, None), step=(1, 1), border_mode='valid',
                 tied_biases=True,
                 prior_mean=0, prior_noise_level=0, **kwargs):
        self.convolution = Convolutional()
        self.mask = Convolutional(name='mask')
        children = [self.convolution, self.mask]
        kwargs.setdefault('children', []).extend(children)
        super(NoisyConvolutional, self).__init__(**kwargs)
        self.filter_size = filter_size
        self.num_filters = num_filters
        self.num_channels = num_channels
        self.noise_batch_size = noise_batch_size
        self.image_size = image_size
        self.step = step
        self.border_mode = border_mode
        self.tied_biases = tied_biases
        self.prior_mean = prior_mean
        self.prior_noise_level = prior_noise_level

    def _push_allocation_config(self):
        self.convolution.filter_size = self.filter_size
        self.convolution.num_filters = self.num_filters
        self.convolution.num_channels = self.num_channels
        # self.convolution.batch_size = self.batch_size
        self.convolution.image_size = self.image_size
        self.convolution.step = self.step
        self.convolution.border_mode = self.border_mode
        self.convolution.tied_biases = self.tied_biases
        self.mask.filter_size = (1, 1)
        self.mask.num_filters = self.num_filters
        self.mask.num_channels = self.num_filters
        # self.mask.batch_size = self.batch_size
        self.mask.image_size = self.convolution.get_dim('output')[1:]
        # self.mask.step = self.step
        # self.mask.border_mode = self.border_mode
        self.mask.tied_biases = self.tied_biases

    def _allocate(self):
        out_shape = self.convolution.get_dim('output')
        N = shared_floatx_zeros((self.noise_batch_size,) + out_shape, name='N')
        add_role(N, NOISE)
        self.parameters.append(N)

    @application(inputs=['input_'], outputs=['output'])
    def apply(self, input_, application_call):
        """Apply the linear transformation followed by masking with noise.
        Parameters
        ----------
        input_ : :class:`~tensor.TensorVariable`
            The input on which to apply the transformations
        Returns
        -------
        output : :class:`~tensor.TensorVariable`
            The transformed input
        """
        from theano.printing import Print

        pre_noise = self.convolution.apply(input_)
        # noise_level = self.mask.apply(input_)
        noise_level = (self.prior_noise_level -
                tensor.clip(self.mask.apply(pre_noise), -16, 16))
        noise_level = copy_and_tag_noise(
                noise_level, self, LOG_SIGMA, 'log_sigma')
        # Allow incomplete batches by just taking the noise that is needed
        noise = self.parameters[0][:noise_level.shape[0], :, :, :]
        # noise = self.theano_rng.normal(noise_level.shape)
        kl = (
            self.prior_noise_level - noise_level 
            + 0.5 * (
                tensor.exp(2 * noise_level)
                + (pre_noise - self.prior_mean) ** 2
                ) / tensor.exp(2 * self.prior_noise_level)
            - 0.5
            )
        application_call.add_auxiliary_variable(kl, roles=[NITS], name='nits')
        return pre_noise + tensor.exp(noise_level) * noise

    def get_dim(self, name):
        if name == 'input_':
            return self.convolution.get_dim(name)
        if name == 'output':
            return self.convolution.get_dim(name)
        if name == 'nits':
            return self.convolution.get_dim('output')
        return super(NoisyConvolutional, self).get_dim(name)

    @property
    def num_output_channels(self):
        return self.num_filters

@contextlib.contextmanager
def training_noise(*bricks):
    r"""Context manager to run noise layers in "training mode".
    """
    # Avoid circular imports.
    from blocks.bricks import BatchNormalization

    bn = find_bricks(bricks, lambda b: isinstance(b, NoiseLayer))
    # Can't use either nested() (deprecated) nor ExitStack (not available
    # on Python 2.7). Well, that sucks.
    try:
        for brick in bn:
            brick.__enter__()
        yield
    finally:
        for brick in bn[::-1]:
            brick.__exit__()

class NoiseLayer(Brick):
    def __init__(self, **kwargs):
        self._training_mode = []
        super(NoiseLayer, self).__init__(**kwargs)

    def __enter__(self):
        self._training_mode.append(True)

    def __exit__(self, *exc_info):
        self._training_mode.pop()


class SpatialNoise(NoiseLayer, Initializable, Random):
    """A learned noise layer.
    """
    @lazy(allocation=['input_dim'])
    def __init__(self, input_dim, noise_batch_size=None, noise_rate=None,
                 tied_noise=False, tied_sigma=False,
                 prior_mean=0, prior_noise_level=0, **kwargs):
        self.mask = Convolutional(name='mask')
        self.flatten = GlobalAverageFlattener() if tied_sigma else None
        children = list(p for p in [self.mask, self.flatten] if p is not None)
        kwargs.setdefault('children', []).extend(children)
        super(SpatialNoise, self).__init__(**kwargs)
        self.input_dim = input_dim
        self.tied_noise = tied_noise
        self.tied_sigma = tied_sigma
        self.noise_batch_size = noise_batch_size
        self.noise_rate = noise_rate if noise_rate is not None else 1.0
        self.prior_mean = prior_mean
        self.prior_noise_level = prior_noise_level
        self._training_mode = []

    def _push_allocation_config(self):
        self.mask.filter_size = (1, 1)
        self.mask.num_filters = self.num_channels
        self.mask.num_channels = self.num_channels
        self.mask.image_size = self.image_size

    def _allocate(self):
        if self.noise_batch_size is not None:
            if self.tied_noise:
                N = shared_floatx_zeros(
                        (self.noise_batch_size, self.input_dim[0]), name='N')
            else:
                N = shared_floatx_zeros(
                        (self.noise_batch_size,) + self.input_dim, name='N')
            add_role(N, NOISE)
            self.parameters.append(N)

    @application(inputs=['input_'], outputs=['output'])
    def apply(self, input_, application_call):
        """Apply the linear transformation followed by masking with noise.
        Parameters
        ----------
        input_ : :class:`~tensor.TensorVariable`
            The input on which to apply the transformations
        Returns
        -------
        output : :class:`~tensor.TensorVariable`
            The transformed input
        """

        # When not in training mode, turn off noise
        if not self._training_mode:
            return input_

        if self.tied_sigma:
            average = tensor.shape_padright(self.flatten.apply(input_), 2)
            noise_level = (self.prior_noise_level -
                    tensor.clip(self.mask.apply(average), -16, 16))
            noise_level = tensor.patternbroadcast(noise_level,
                    (False, False, True, True))
            noise_level = copy_and_tag_noise(
                    noise_level, self, LOG_SIGMA, 'log_sigma')
        else:
            average = input_
            noise_level = (self.prior_noise_level -
                    tensor.clip(self.mask.apply(input_), -16, 16))
            noise_level = copy_and_tag_noise(
                    noise_level, self, LOG_SIGMA, 'log_sigma')
        # Allow incomplete batches by just taking the noise that is needed
        if self.tied_noise:
            if self.noise_batch_size is not None:
                noise = self.parameters[0][:input_.shape[0], :]
            else:
                noise = self.theano_rng.normal(input_.shape[0:2])
            noise = tensor.shape_padright(2)
        else:
            if self.noise_batch_size is not None:
                noise = self.parameters[0][:input_.shape[0], :, :, :]
            else:
                noise = self.theano_rng.normal(input_.shape)
        kl = (
            self.prior_noise_level - noise_level
            + 0.5 * (
                tensor.exp(2 * noise_level)
                + (average - self.prior_mean) ** 2
                ) / tensor.exp(2 * self.prior_noise_level)
            - 0.5
            )
        application_call.add_auxiliary_variable(kl, roles=[NITS], name='nits')
        return input_ + self.noise_rate * tensor.exp(noise_level) * noise

    # Needed for the Feedforward interface.
    @property
    def output_dim(self):
        return self.input_dim

    # The following properties allow for BatchNormalization bricks
    # to be used directly inside of a ConvolutionalSequence.
    @property
    def image_size(self):
        return self.input_dim[-2:]

    @image_size.setter
    def image_size(self, value):
        if not isinstance(self.input_dim, collections.Sequence):
            self.input_dim = (None,) + tuple(value)
        else:
            self.input_dim = (self.input_dim[0],) + tuple(value)

    @property
    def num_channels(self):
        return self.input_dim[0]

    @num_channels.setter
    def num_channels(self, value):
        if not isinstance(self.input_dim, collections.Sequence):
            self.input_dim = (value,) + (None, None)
        else:
            self.input_dim = (value,) + self.input_dim[-2:]

    def get_dim(self, name):
        if name in ('input', 'output'):
            return self.input_dim
        else:
            raise KeyError

    @property
    def num_output_channels(self):
        return self.num_channels

class NoisyAveragePredictor(object):
    def __init__(self, probs, labels, noise_params):
        self.probs = probs
        self.labels = labels 
        self.data_stream = data_stream
        self.noise_params = noise_params

    def compile(self):
        pass

    def evaluate(self, data_stream):
        pass

class NoisyDataStreamMonitoring(DataStreamMonitoring):
    def __init__(self, variables, data_stream,
            updates=None, noise_parameters=None, **kwargs):
        kwargs.setdefault("after_epoch", True)
        kwargs.setdefault("before_first_epoch", True)
        super(DataStreamMonitoring, self).__init__(**kwargs)
        self._evaluator = DatasetEvaluator(variables, updates)
        self.data_stream = data_stream
        self.noise_parameters = noise_parameters

    def do(self, callback_name, *args):
        """Write the values of monitored variables to the log."""
        logger.info("Monitoring on auxiliary data started")
        saved = [(p, p.get_value()) for p in self.noise_parameters]
        for (p, v) in saved:
            p.set_value(np.zeros(v.shape, dtype=v.dtype))
        value_dict = self._evaluator.evaluate(self.data_stream)
        self.add_records(self.main_loop.log, value_dict.items())
        for (p, v) in saved:
            p.set_value(v)
        logger.info("Monitoring on auxiliary data finished")


class NoisyLeNet(FeedforwardSequence, Initializable):
    """LeNet-like convolutional network.

    The class implements LeNet, which is a convolutional sequence with
    an MLP on top (several fully-connected layers). For details see
    [LeCun95]_.

    .. [LeCun95] LeCun, Yann, et al.
       *Comparison of learning algorithms for handwritten digit
       recognition.*,
       International conference on artificial neural networks. Vol. 60.

    Parameters
    ----------
    conv_activations : list of :class:`.Brick`
        Activations for convolutional network.
    num_channels : int
        Number of channels in the input image.
    image_shape : tuple
        Input image shape.
    filter_sizes : list of tuples
        Filter sizes of :class:`.blocks.conv.ConvolutionalLayer`.
    feature_maps : list
        Number of filters for each of convolutions.
    pooling_sizes : list of tuples
        Sizes of max pooling for each convolutional layer.
    top_mlp_activations : list of :class:`.blocks.bricks.Activation`
        List of activations for the top MLP.
    top_mlp_dims : list
        Numbers of hidden units and the output dimension of the top MLP.
    conv_step : tuples
        Step of convolution (similar for all layers).
    border_mode : str
        Border mode of convolution (similar for all layers).

    """
    def __init__(self, conv_activations, num_channels, image_shape,
                 noise_batch_size,
                 filter_sizes, feature_maps, pooling_sizes,
                 top_mlp_activations, top_mlp_dims,
                 conv_step=None, border_mode='valid',
                 tied_biases=True, **kwargs):
        if conv_step is None:
            self.conv_step = (1, 1)
        else:
            self.conv_step = conv_step
        self.num_channels = num_channels
        self.image_shape = image_shape
        self.noise_batch_size = noise_batch_size
        self.top_mlp_activations = top_mlp_activations
        self.top_mlp_dims = top_mlp_dims
        self.border_mode = border_mode
        self.tied_biases = tied_biases

        conv_parameters = zip(filter_sizes, feature_maps)

        # Construct convolutional layers with corresponding parameters
        self.layers = list(interleave([
            (NoisyConvolutional(filter_size=filter_size,
                           num_filters=num_filter,
                           step=self.conv_step,
                           border_mode=self.border_mode,
                           tied_biases=self.tied_biases,
                           noise_batch_size=self.noise_batch_size,
                           name='conv_{}'.format(i))
             for i, (filter_size, num_filter)
             in enumerate(conv_parameters)),
            conv_activations,
            (MaxPooling(size, name='pool_{}'.format(i))
             for i, size in enumerate(pooling_sizes))]))

        self.conv_sequence = ConvolutionalSequence(
                self.layers, num_channels,
                image_size=image_shape)
        self.conv_sequence.name = 'cs'

        # Construct a top MLP
        self.top_mlp = MLP(top_mlp_activations, top_mlp_dims,
                prototype=NoisyLinear(noise_batch_size=self.noise_batch_size))

        # We need to flatten the output of the last convolutional layer.
        # This brick accepts a tensor of dimension (batch_size, ...) and
        # returns a matrix (batch_size, features)
        self.flattener = Flattener()
        application_methods = [self.conv_sequence.apply, self.flattener.apply,
                               self.top_mlp.apply]
        super(NoisyLeNet, self).__init__(application_methods, **kwargs)

    @property
    def output_dim(self):
        return self.top_mlp_dims[-1]

    @output_dim.setter
    def output_dim(self, value):
        self.top_mlp_dims[-1] = value

    def _push_allocation_config(self):
        self.conv_sequence._push_allocation_config()
        conv_out_dim = self.conv_sequence.get_dim('output')

        self.top_mlp.activations = self.top_mlp_activations
        self.top_mlp.dims = [np.prod(conv_out_dim)] + self.top_mlp_dims


def create_noisy_lenet_5(noise_batch_size):
    feature_maps = [6, 16]
    mlp_hiddens = [120, 84]
    conv_sizes = [5, 5]
    pool_sizes = [2, 2]
    image_size = (28, 28)
    output_size = 10

    # The above are from LeCun's paper. The blocks example had:
    #    feature_maps = [20, 50]
    #    mlp_hiddens = [500]

    # Use ReLUs everywhere and softmax for the final prediction
    conv_activations = [Rectifier() for _ in feature_maps]
    mlp_activations = [Rectifier() for _ in mlp_hiddens] + [Softmax()]
    convnet = NoisyLeNet(conv_activations, 1, image_size, noise_batch_size,
                    filter_sizes=zip(conv_sizes, conv_sizes),
                    feature_maps=feature_maps,
                    pooling_sizes=zip(pool_sizes, pool_sizes),
                    top_mlp_activations=mlp_activations,
                    top_mlp_dims=mlp_hiddens + [output_size],
                    border_mode='valid',
                    weights_init=Constant(0), # Uniform(width=.2),
                    biases_init=Constant(0))

    # We push initialization config to set different initialization schemes
    # for convolutional layers.
    convnet.push_initialization_config()
    convnet.layers[0].convolution.weights_init = (
            Uniform(width=.2))
    convnet.layers[3].convolution.weights_init = (
            Uniform(width=.09))
    convnet.top_mlp.linear_transformations[0].linear.weights_init = (
            Uniform(width=.08))
    convnet.top_mlp.linear_transformations[1].linear.weights_init = (
            Uniform(width=.11))
    convnet.top_mlp.linear_transformations[2].linear.weights_init = (
            Uniform(width=.2))
#
#    convnet.layers[0].mask.weights_init = (
#            Uniform(width=.2))
#    convnet.layers[3].mask.weights_init = (
#            Uniform(width=.09))
#    convnet.top_mlp.linear_transformations[0].mask.weights_init = (
#            Uniform(width=.08))
#    convnet.top_mlp.linear_transformations[1].mask.weights_init = (
#            Uniform(width=.11))
#    convnet.top_mlp.linear_transformations[2].mask.weights_init = (
#            Uniform(width=.2))

#    convnet.layers[0].mask.bias_init = (
#            Constant(8))
#    convnet.layers[3].mask.bias_init = (
#            Constant(8))
#    convnet.top_mlp.linear_transformations[0].mask.bias_init = (
#            Constant(8))
#    convnet.top_mlp.linear_transformations[1].mask.bias_init = (
#            Constant(8))
#    convnet.top_mlp.linear_transformations[2].mask.bias_init = (
#            Constant(8))

    convnet.initialize()

    return convnet

class SampledScheme(BatchScheme):
    """Sampled batches iterator.
    Like shuffledScheme but uses a sampling method instead, and makes
    the final batch complete.
    """
    def __init__(self, *args, **kwargs):
        self.rng = kwargs.pop('rng', None)
        if self.rng is None:
            self.rng = np.random.RandomState(fuel.config.default_seed)
        super(SampledScheme, self).__init__(*args, **kwargs)

    def get_request_iterator(self):
        indices = list(self.indices)
        count = len(indices)
        if count < self.batch_size:
            count = self.batch_size
        indices = self.rng.choice(indices, count)
        return imap(list, partition_all(self.batch_size, indices))
