from itertools import chain, combinations
import tensorflow as tf


def rank(x):
    assert isinstance(x, tf.Tensor)
    return x.shape.ndims


def shape(x):
    assert isinstance(x, tf.Tensor)
    return tuple(-1 if dims.value is None else dims.value for dims in x.shape.dims)


def product(xs):
    prod = 1
    for x in xs:
        prod *= x
    return prod


def make_least_common_shape(xs, ignore_ranks=()):
    assert len(xs) > 1
    shapes = [shape(x) for x in xs]
    common_rank = len(shapes[0])
    assert all(len(s) == common_rank for s in shapes)
    ref_shape = tuple(max(s[r] for s in shapes) for r in range(common_rank))
    ys = list()
    for x, s in zip(xs, shapes):
        multiples = [ref_dims if dims == 1 and r not in ignore_ranks else 1 for r, (dims, ref_dims) in enumerate(zip(s, ref_shape))]
        if not all(m == 1 for m in multiples):
            x = tf.tile(input=x, multiples=multiples)
        assert rank(x) == common_rank and all(d1 == d2 for r, (d1, d2) in enumerate(zip(shape(x), ref_shape)) if r not in ignore_ranks)
        ys.append(x)
    return ys


def make_broadcastable(xs):
    assert len(xs) > 0
    if len(xs) == 1 and not isinstance(xs[0], tf.Tensor):
        xs = xs[0]
    shapes = [shape(x) for x in xs]
    ref_shape = max(shapes, key=(lambda s: len(s) - sum(dims == 1 for dims in s) / (len(s) + 1)))
    ys = list()
    for x, s in zip(xs, shapes):
        s = list(s)
        if len(s) < len(ref_shape):
            last_dims = None
            for r, (dims, ref_dims) in enumerate(zip(s, ref_shape)):
                if r < len(s) and dims in (ref_dims, 1):
                    last_dims = dims
                else:
                    assert dims != last_dims
                    x = tf.expand_dims(input=x, axis=r)
                    s.insert(r, 1)
        assert rank(x) == len(ref_shape) and all(d1 == d2 or d1 == 1 for d1, d2 in zip(shape(x), ref_shape))
        ys.append(x)
    return ys


class Model(object):

    precision = 32
    current = None

    @staticmethod
    def dtype(dtype, include_bytes=False):
        assert Model.precision % 8 == 0
        assert dtype in ('float', 'int', 'bool')
        if dtype == 'float':
            if Model.precision == 32:
                dtype = tf.float32
            else:
                assert False
        elif dtype == 'int':
            if Model.precision == 32:
                dtype = tf.int32
            else:
                assert False
        elif dtype == 'bool':
            dtype = tf.bool
        else:
            assert False
        if include_bytes:
            return dtype, Model.precision // 8
        else:
            return dtype

    def __init__(self, name=None, optimizer='adam', learning_rate=0.001, weight_decay=None, clip_gradients=None, model_directory=None, summary_directory=None):
        assert name is None or isinstance(name, str)
        assert optimizer in ('adam',)
        assert isinstance(learning_rate, float)
        assert weight_decay is None or isinstance(weight_decay, float)
        assert clip_gradients is None or isinstance(clip_gradients, float)
        assert model_directory is None or isinstance(model_directory, str)
        assert summary_directory is None or isinstance(summary_directory, str)
        self.name = name
        self.optimizer = optimizer
        self.learning_rate = learning_rate
        self.weight_decay = weight_decay
        self.clip_gradients = clip_gradients
        self.model_directory = model_directory
        self.summary_directory = summary_directory
        self.tensors = dict()
        self.variables = dict()
        self.placeholders = dict()
        self.num_parameters = 0
        self.num_bytes = 0
        self.scope = None
        self.session = None
        self.coordinator = None
        self.defined = False
        self.optimization = None

    def __str__(self):
        if self.name is None:
            return 'Model'
        else:
            return self.name

    def register_tensor(self, key, tensor):
        assert key not in ('loss', 'dropout')
        assert key not in self.tensors
        self.tensors[key] = tensor

    def register_variable(self, key, variable, num_parameters, num_bytes):
        if key in self.variables:
            assert variable == self.variables[key]
        else:
            self.variables[key] = variable
            self.num_parameters += num_parameters
            self.num_bytes += num_bytes

    def register_placeholder(self, key, placeholder):
        assert key not in self.placeholders
        self.placeholders[key] = placeholder

    def __enter__(self):
        tf.reset_default_graph()
        assert Model.current is None
        Model.current = self
        self.scope = tf.variable_scope(str(self))
        self.scope.__enter__()
        Input(name='training', shape=(), dtype='bool', batched=False).forward()
        self.training = self.placeholders.pop('training')
        Input(name='dropout', shape=(), batched=False).forward()
        self.dropout = self.placeholders.pop('dropout')
        return self

    def __exit__(self, type, value, tb):
        if type is not None:
            if self.scope is not None:
                self.scope.__exit__(None, None, None)
            if self.coordinator is not None:
                self.coordinator.request_stop()
                self.coordinator.join(threads=self.queue_threads)
            if self.session is not None:
                self.session.close()
            Model.current = None
            raise
        if self.defined:
            self.coordinator.request_stop()
            self.coordinator.join(threads=self.queue_threads)
            self.save()
            self.session.close()
        else:
            if self.weight_decay is not None and self.weight_decay > 0.0:
                for name, variable in self.variables.items():
                    regularization = self.weight_decay * tf.nn.l2_loss(t=variable, name=(name + '-regularization'))
                    tf.losses.add_loss(loss=regularization, loss_collection=tf.GraphKeys.REGULARIZATION_LOSSES)
            loss = tf.losses.get_total_loss()
            self.tensors['loss'] = loss
            if self.optimizer == 'adam':
                optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
            try:
                grads_and_vars = optimizer.compute_gradients(loss=loss)
                if self.clip_gradients is not None:
                    grads_and_vars = [(tf.clip_by_value(t=grad, clip_value_min=-self.clip_gradients, clip_value_max=self.clip_gradients), var) for grad, var in grads_and_vars]
                self.optimization = optimizer.apply_gradients(grads_and_vars=grads_and_vars)
            except ValueError as exc:
                if str(exc) == 'No variables to optimize.':
                    if self.optimization is None:
                        self.optimization = tf.no_op()
                else:
                    raise exc
            self.scope.__exit__(type, value, tb)
        assert Model.current is not None
        Model.current = None

    def finalize(self, restore=False):
        assert not self.defined
        if self.weight_decay is not None and self.weight_decay > 0.0:
            for name, variable in self.variables.items():
                regularization = self.weight_decay * tf.nn.l2_loss(t=variable, name=(name + '-regularization'))
                tf.losses.add_loss(loss=regularization, loss_collection=tf.GraphKeys.REGULARIZATION_LOSSES)
        loss = tf.losses.get_total_loss()
        self.tensors['loss'] = loss
        if self.optimizer == 'adam':
            optimizer = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
        try:
            grads_and_vars = optimizer.compute_gradients(loss=loss)
            if self.clip_gradients is not None:
                grads_and_vars = [(tf.clip_by_value(t=grad, clip_value_min=-self.clip_gradients, clip_value_max=self.clip_gradients), var) for grad, var in grads_and_vars]
            self.optimization = optimizer.apply_gradients(grads_and_vars=grads_and_vars)
        except ValueError as exc:
            if str(exc) == 'No variables to optimize.':
                if self.optimization is None:
                    self.optimization = tf.no_op()
            else:
                raise exc
        global_variables_initializer = tf.global_variables_initializer()
        if self.model_directory is not None:
            self.saver = tf.train.Saver()
        if self.summary_directory is not None:
            tf.summary.scalar(name='loss', tensor=loss)
            for variable in tf.trainable_variables():
                tf.summary.histogram(name=variable.name, values=variable)
            self.summaries = tf.summary.merge_all()
        self.scope.__exit__(None, None, None)
        self.scope = None
        tf.get_default_graph().finalize()
        self.defined = True

        self.session = tf.Session()
        if restore:
            assert self.model_directory
            # save_path = tf.train.latest_checkpoint(checkpoint_dir=self.model_directory)
            self.saver.restore(sess=self.session, save_path=(self.model_directory + 'model'))
        else:
            self.session.run(fetches=global_variables_initializer)
        if self.summary_directory is not None:
            self.summary_writer = tf.summary.FileWriter(logdir=self.summary_directory, graph=self.session.graph)
        self.coordinator = tf.train.Coordinator()
        self.queue_threads = tf.train.start_queue_runners(sess=self.session, coord=self.coordinator)

    def save(self):
        assert self.defined
        if self.model_directory:
            self.saver.save(sess=self.session, save_path=(self.model_directory + 'model'))

    def __call__(self, query=None, data=None, optimize=False, summarize=False, dropout=None):
        assert self.session
        if query is None:
            fetches = dict()
        elif isinstance(query, str):
            fetches = dict(query=self.tensors[query])
        else:
            fetches = {name: self.tensors[name] for name in query}
        if data is None:
            feed_dict = dict()
        elif isinstance(data, dict):
            feed_dict = {self.placeholders[name]: value for name, value in data.items() if name in self.placeholders}
        else:
            assert len(self.placeholders) == 1
            feed_dict = {next(iter(self.placeholders.values())): data}
        if optimize:
            feed_dict[self.training] = True
            assert 'optimization' not in fetches
            fetches['optimization'] = self.optimization
        else:
            feed_dict[self.training] = False
        if self.summary_directory is not None and summarize:
            assert 'summaries' not in fetches
            fetches['summaries'] = self.summaries
        assert dropout is None or 0.0 <= dropout < 1.0
        if dropout is None:
            feed_dict[self.dropout] = 0.0
        else:
            feed_dict[self.dropout] = dropout
        fetched = self.session.run(fetches=fetches, feed_dict=feed_dict)
        if optimize:
            fetched.pop('optimization')
        if self.summary_directory is not None and summarize:
            fetched.pop('summaries')
        return fetched


class Unit(object):

    num_in = None
    num_out = None

    index = 0

    def __init__(self, name=None, template=True):
        assert Model.current is not None
        assert self.num_in is not None and self.num_out is not None
        assert name is None or isinstance(name, str)
        if name is None:
            name = self.__class__.__name__ + str(self.__class__.index)
            self.__class__.index += 1
        self.name = name
        self.initialized = False
        self.outputs = dict()
        if template:
            self.fn_forward = tf.make_template(name_=str(self), func_=self.forward, create_scope_now_=True)
        else:
            self.fn_forward = self.forward

    def __str__(self):
        return self.name

    def __repr__(self):
        return str(self)

    def initialize(self, *xs):
        assert not self.initialized
        self.initialized = True

    def forward(self, *xs):
        # try:
        #     assert any(self.num_in == num_in for num_in in self.__class__.num_in)
        # except TypeError:
        #     assert self.num_in == self.__class__.num_in
        assert len(xs) == self.num_in or self.num_in == -1, (len(xs), self.num_in)

        if not self.initialized:
            self.initialize(*xs)

    def __call__(self, inputs=(), output_key=None):
        assert output_key is None or isinstance(output_key, str)
        if output_key is not None and output_key in self.outputs:
            return self.outputs[output_key]
        output = self.fn_forward(*inputs)
        if isinstance(output, tf.Tensor):
            if output_key is not None:
                self.outputs[output_key] = output
                Model.current.register_tensor(key=output_key, tensor=output)
        elif len(output) == 1:
            output = output[0]
            if output_key is not None:
                self.outputs[output_key] = output
                Model.current.register_tensor(key=output_key, tensor=output)
        else:
            output = tuple(output)
            if output_key is not None:
                self.outputs[output_key] = output
                for n, tensor in enumerate(output):
                    Model.current.register_tensor(key=(output_key + str(n)), tensor=tensor)
        return output

    def __rshift__(self, other):
        assert isinstance(other, Unit)
        if self.num_in == 0:
            assert self.num_out == other.num_in or other.num_in == -1, (self.num_out, other.num_in)
            inputs = self()
            inputs = (inputs,) if isinstance(inputs, tf.Tensor) else inputs
            return other(inputs=inputs)
        else:
            return Composed(first=self, second=other)

    def __rrshift__(self, other):
        if isinstance(other, tf.Tensor):
            return self(inputs=(other,))
        inputs = list()
        composed = False
        for x in other:
            if isinstance(x, tf.Tensor):
                inputs.append(x)
            elif x.num_in == 0:
                x = x()
                assert isinstance(x, tf.Tensor)
                inputs.append(x)
            elif isinstance(x, Unit):
                inputs.append(x)
                composed = True
            else:
                assert False
                # x = x()
                # assert isinstance(x, tf.Tensor)
                # inputs.append(x)
        if composed:
            return Composed(first=inputs, second=self)
        else:
            assert len(inputs) == self.num_in or self.num_in == -1, (len(inputs), self.num_in)
            return self(inputs=inputs)


class Composed(Unit):

    def __init__(self, first, second):
        if isinstance(first, Unit):
            assert first.num_out == second.num_in or second.num_in == -1, (first.num_out, second.num_in)
            self.num_in = first.num_in
        else:
            assert all(isinstance(unit, tf.Tensor) or unit.num_out == 1 for unit in first)
            assert len(first) == second.num_in or second.num_in == -1, (len(first), second.num_in)
            self.num_in = 1
        self.num_out = second.num_out
        super(Composed, self).__init__(template=False)
        self.first = first
        self.second = second

    def __str__(self):
        return '({} -> {})'.format(self.first, self.second)

    def initialize(self, *xs):
        assert not self.initialized
        self.initialized = True

    def forward(self, *xs):
        super(Composed, self).forward(*xs)
        # assert isinstance(self.first, Unit) or len(xs) == 1
        if isinstance(self.first, Unit):
            assert all(isinstance(x, tf.Tensor) for x in xs)
            if len(xs) == 1:
                xs = xs[0]
            return xs >> self.first >> self.second
        else:
            assert all(isinstance(x, tf.Tensor) for x in xs)
            if len(xs) == 1:
                xs = tuple(unit if isinstance(unit, tf.Tensor) else xs[0] >> unit for unit in self.first)
            else:
                xs = tuple(unit if isinstance(unit, tf.Tensor) else xs >> unit for unit in self.first)
            assert all(isinstance(x, tf.Tensor) for x in xs)
            return xs >> self.second

    def __rshift__(self, other):
        assert isinstance(other, Unit)
        return Composed(first=self, second=other)


# Create a custom class with some arguments specified
def customize(unit_, **specified):

    class CustomUnit(unit_):

        def __init__(self, **kwargs):
            assert all(arg not in kwargs for arg in specified)
            kwargs.update(specified)
            if kwargs.get('name') is None:
                kwargs['name'] = unit_.__name__ + str(unit_.index)
                unit_.index += 1
            super(CustomUnit, self).__init__(**kwargs)

    return CustomUnit


class Layer(Unit):

    num_in = 1
    num_out = 1

    def __init__(self, size, name=None):
        super(Layer, self).__init__(name=name)
        assert self.__class__.num_in == self.__class__.num_out
        assert isinstance(size, int) and size >= 0
        if size == 0:
            size = 1
            self.squeeze = True
        else:
            self.squeeze = False
        self.size = size


class LayerStack(Unit):

    num_in = 1
    num_out = 1

    def initialize(self, *xs):
        super(LayerStack, self).initialize(*xs)
        self.layers = list()

    def forward(self, *xs):
        super(LayerStack, self).forward(*xs)
        for layer in self.layers:
            xs >>= layer
        return xs


class Variable(Unit):

    num_in = 0
    num_out = 1

    def __init__(self, name, shape=None, dtype='float', init='out', value=None):
        super(Variable, self).__init__(name=name)
        assert self.__class__.num_in == 0 and self.__class__.num_out == 1
        assert isinstance(name, str)
        if shape is not None:
            shape = (shape,) if isinstance(shape, int) else tuple(shape)
            assert len(shape) > 0 and all(isinstance(n, int) and n > 0 for n in shape)
        assert init in ('constant', 'zeros', 'ones', 'in', 'out', 'in-out', 'stddev') or Activation.valid(init)
        assert init in ('constant', 'zeros', 'ones') or dtype == 'float'
        self.shape = shape
        self.dtype, self.dtype_bytes = Model.dtype(dtype=dtype, include_bytes=True)
        self.init = init
        self.value = value

    def specify_shape(self, shape):
        if self.shape is None:
            self.shape = shape
        else:
            assert self.shape == shape

    def forward(self):
        super(Variable, self).forward()
        # TODO: own instead of tf.contrib.layers.variance_scaling_initializer, and with min(?, 0.01)
        assert self.shape is not None
        if self.init == 'zeros':
            initializer = tf.zeros_initializer(dtype=self.dtype)
        elif self.init == 'ones':
            initializer = tf.ones_initializer(dtype=self.dtype)
        elif self.init == 'stddev':
            assert self.value is not None
            initializer = tf.random_normal_initializer(mean=0.0, stddev=self.value, dtype=tf.float32)
        elif self.init == 'selu':
            initializer = tf.contrib.layers.variance_scaling_initializer(factor=1.0, mode='FAN_OUT', dtype=self.dtype)
        elif self.init == 'out':
            initializer = tf.contrib.layers.variance_scaling_initializer(factor=2.0, mode='FAN_OUT', dtype=self.dtype)
        elif self.init == 'in' or self.init in ('elu', 'relu'):
            assert len(self.shape) >= 2
            initializer = tf.contrib.layers.variance_scaling_initializer(factor=2.0, mode='FAN_IN', dtype=self.dtype)
        elif self.init == 'in-out' or Activation.valid(self.init):
            assert len(self.shape) >= 2
            initializer = tf.contrib.layers.variance_scaling_initializer(factor=1.0, mode='FAN_AVG', dtype=self.dtype)
        else:
            assert False
        variable = tf.get_variable(name=str(self), shape=self.shape, dtype=self.dtype, initializer=initializer)
        num_parameters = product(self.shape)
        num_bytes = num_parameters * self.dtype_bytes
        Model.current.register_variable(key='{}/{}'.format(tf.get_variable_scope().name, str(self)), variable=variable, num_parameters=num_parameters, num_bytes=num_bytes)
        return tf.identity(input=variable)


class Linear(Layer):

    def __init__(self, size, bias=True, name=None):
        super(Linear, self).__init__(size=size, name=name)
        assert isinstance(bias, bool)
        self.weights = None
        self.bias = bias

    def initialize(self, x):
        super(Linear, self).initialize(x)
        if rank(x) == 2:
            self.weights = Variable(name='weights', shape=(shape(x)[-1], self.size), init='in-out')
        elif rank(x) == 3:
            self.weights = Variable(name='weights', shape=(1, shape(x)[-1], self.size), init='in-out')
        elif rank(x) == 4:
            self.weights = Variable(name='weights', shape=(1, 1, shape(x)[-1], self.size), init='in-out')
        self.bias = Variable(name='bias', shape=self.size, init='zeros') if self.bias else None

    def forward(self, x):
        super(Linear, self).forward(x)
        assert 2 <= rank(x) <= 4
        if rank(x) == 2:
            x = tf.matmul(a=x, b=self.weights())
        elif rank(x) == 3:
            x = tf.nn.conv1d(value=x, filters=self.weights(), stride=1, padding='SAME')
        elif rank(x) == 4:
            x = tf.nn.conv2d(input=x, filter=self.weights(), strides=(1, 1, 1, 1), padding='SAME')
        if self.bias is not None:
            x = tf.nn.bias_add(value=x, bias=self.bias())
        if self.squeeze:
            x = tf.squeeze(input=x, axis=-1)
        return x


class Input(Unit):

    num_in = 0
    num_out = 1

    def __init__(self, name, shape, dtype='float', batched=True, tensor=None):
        super(Input, self).__init__(name=name)
        assert isinstance(name, str)
        shape = (shape,) if isinstance(shape, int) else tuple(shape)
        assert all(isinstance(n, int) and (n > 0 or n == -1) for n in shape)
        assert isinstance(batched, bool)
        self.shape = tuple(None if x == -1 else x for x in shape)
        self.dtype = Model.dtype(dtype=dtype)
        if batched:
            self.shape = (None,) + self.shape
        self.tensor = tensor

    def forward(self):
        super(Input, self).forward()
        if self.tensor is None:
            placeholder = tf.placeholder(dtype=self.dtype, shape=self.shape, name=str(self))
            Model.current.register_placeholder(key=str(self), placeholder=placeholder)
            self.tensor = tf.identity(input=placeholder)
        return self.tensor


class Output(Unit):

    num_in = 1
    num_out = 2

    def __init__(self, name, shape, dtype='float', batched=True, tensor=None):
        super(Output, self).__init__(name=name)
        assert isinstance(name, str)
        self.shape = shape
        self.dtype = dtype
        self.batched = batched
        self.tensor = tensor

    def initialize(self, x):
        super(Output, self).initialize(x)
        self.input = Input(name=str(self), shape=self.shape, dtype=self.dtype, batched=self.batched, tensor=self.tensor)


class Binary(Output):

    def __init__(self, name, binary_transform=True, soft=0.0, tensor=None):
        super(Binary, self).__init__(name=name, shape=(), tensor=tensor)
        assert isinstance(binary_transform, bool)
        assert isinstance(soft, float) and 0.0 <= soft < 0.5
        self.binary_transform = binary_transform
        self.soft = soft

    def initialize(self, x):
        super(Binary, self).initialize(x)
        self.linear = Linear(size=0)

    def forward(self, x):
        super(Binary, self).forward(x)
        correct = self.input()
        if self.soft > 0.0:
            noise = tf.random_uniform(shape=tf.shape(input=correct), minval=0.0, maxval=self.soft)
            soft_correct = tf.abs(x=(correct - noise))
        else:
            soft_correct = correct
        if self.binary_transform:
            x >>= self.linear
            x = (tf.tanh(x=x) + 1.0) / 2.0
        cross_entropy = -(soft_correct * tf.log(x=tf.maximum(x=x, y=1e-8)) + (1.0 - soft_correct) * tf.log(x=tf.maximum(x=(1.0 - x), y=1e-8)))
        loss = tf.reduce_mean(input_tensor=cross_entropy)
        tf.losses.add_loss(loss=loss)
        prediction = tf.cast(x=tf.greater(x=x, y=tf.constant(value=0.5)), dtype=Model.dtype('float'))
        num_correct = tf.cast(x=tf.equal(x=prediction, y=correct), dtype=Model.dtype('float'))
        accuracy = tf.reduce_mean(input_tensor=num_correct)
        Model.current.register_tensor(key=(str(self) + '_accuracy'), tensor=accuracy)
        return correct, prediction


class Classification(Output):

    def __init__(self, name, num_classes, multi_class=False, soft=0.0, tensor=None):
        super(Classification, self).__init__(name=name, shape=(num_classes,), tensor=tensor)
        assert isinstance(num_classes, int) and num_classes > 0
        assert isinstance(multi_class, bool)
        assert isinstance(soft, float) and 0.0 <= soft < 0.5
        self.num_classes = num_classes
        self.multi_class = multi_class
        self.soft = soft

    def initialize(self, x):
        super(Classification, self).initialize(x)
        self.linear = Linear(size=self.num_classes)

    def forward(self, x):
        super(Classification, self).forward(x)
        correct = self.input()
        if not self.multi_class and rank(correct) == 1:
            correct_onehot = tf.one_hot(indices=correct, depth=self.num_classes)
        else:
            correct_onehot = correct
        if self.soft > 0.0:
            noise = tf.random_uniform(shape=(1, shape(correct_onehot)[1]), minval=0.0, maxval=self.soft)
            soft_correct = tf.abs(x=(correct_onehot - noise))
        else:
            soft_correct = correct_onehot
        x >>= self.linear
        if self.multi_class:
            tf.losses.sigmoid_cross_entropy(multi_class_labels=soft_correct, logits=x)
        else:
            tf.losses.softmax_cross_entropy(onehot_labels=soft_correct, logits=x)
        prediction = tf.argmax(input=x, axis=1)
        prediction_onehot = tf.one_hot(indices=prediction, depth=self.num_classes)
        if self.multi_class or rank(correct) == 2:
            prediction = prediction_onehot
        relevant = tf.reduce_sum(input_tensor=correct, axis=1)
        selected = tf.reduce_sum(input_tensor=prediction_onehot, axis=1)
        true_positive = tf.reduce_sum(input_tensor=tf.minimum(x=prediction_onehot, y=correct), axis=1)
        precision = tf.reduce_mean(input_tensor=tf.divide(x=true_positive, y=selected), axis=0)
        recall = tf.reduce_mean(input_tensor=tf.divide(x=true_positive, y=relevant), axis=0)
        fscore = (2 * precision * recall) / (precision + recall)
        Model.current.register_tensor(key=(str(self) + '_precision'), tensor=precision)
        Model.current.register_tensor(key=(str(self) + '_recall'), tensor=recall)
        Model.current.register_tensor(key=(str(self) + '_fscore'), tensor=fscore)
        return correct, prediction


class Distance(Output):

    def __init__(self, name, shape, tensor=None):
        super(Distance, self).__init__(name=name, shape=shape, tensor=tensor)

    def forward(self, x):
        super(Distance, self).forward(x)
        correct = self.input()
        prediction = x
        tf.losses.mean_squared_error(labels=correct, predictions=prediction)
        return correct, prediction


class Identity(Unit):

    num_in = 1
    num_out = 1

    def forward(self, *xs):
        super(Identity, self).forward(*xs)
        assert len(xs) >= 1
        return xs[0] if len(xs) == 1 else xs


class Print(Unit):

    num_in = -1
    num_out = -1

    def __init__(self, size=10, times=None, prefix=None, name=None):
        super(Print, self).__init__(name=name)
        assert isinstance(size, int) and size > 0
        assert times is None or isinstance(times, int) and times > 0
        assert prefix is None or isinstance(prefix, str)
        self.size = size
        self.times = times
        self.prefix = prefix

    def forward(self, *xs):
        super(Print, self).forward(*xs)
        if self.prefix is None or self.prefix[-2:] == ': ':
            message = self.prefix
        elif self.prefix[-1] == ':':
            message = self.prefix + ' '
        else:
            message = self.prefix + ': '
        return (tf.Print(input_=xs[0], data=xs, message=message, first_n=self.times, summarize=self.size),) + tuple(xs[1:])


class Constant(Unit):

    num_in = 1
    num_out = 1

    def __init__(self, value, dtype, name=None):
        super(Constant, self).__init__(name=name)
        self.value = value
        self.dtype = dtype

    def forward(self, x):
        super(Constant, self).forward(x)
        batch_size = tf.shape(input=x)[0]
        x = tf.constant(value=self.value, dtype=Model.dtype(self.dtype))
        multiples = (batch_size,) + tuple(1 for _ in range(rank(x)))
        return tf.tile(input=tf.expand_dims(input=x, axis=0), multiples=multiples)


class Select(Unit):

    num_in = -1
    num_out = 1

    def __init__(self, index, name=None):
        super(Select, self).__init__(name=name)
        assert isinstance(index, int) and index >= 0
        self.index = index

    def forward(self, *xs):
        super(Select, self).forward(*xs)
        assert len(xs) > self.index
        return xs[self.index]


class Activation(Unit):

    num_in = 1
    num_out = 1

    @staticmethod
    def valid(activation):
        return activation in ('elu', 'relu', 'sigmoid', 'softmax', 'tanh')

    def __init__(self, activation='relu', name=None):
        super(Activation, self).__init__(name=name)
        assert Activation.valid(activation)
        self.activation = activation

    def forward(self, x):
        super(Activation, self).forward(x)
        if self.activation == 'elu':
            return tf.nn.elu(features=x)
        elif self.activation == 'relu':
            return tf.nn.relu(features=x)
        elif self.activation == 'selu':
            # https://arxiv.org/pdf/1706.02515.pdf
            alpha = 1.6732632423543772848170429916717
            scale = 1.0507009873554804934193349852946
            return scale * tf.where(condition=(x >= 0.0), x=x, y=(alpha * tf.nn.elu(features=x)))
        elif self.activation == 'sigmoid':
            return tf.sigmoid(x=x)
        elif self.activation == 'softmax':
            return tf.nn.softmax(logits=x)
        elif self.activation == 'tanh':
            return tf.nn.tanh(x=x)


class Dropout(Unit):

    num_in = 1
    num_out = 1

    def forward(self, x):
        super(Dropout, self).forward(x)
        return tf.nn.dropout(x=x, keep_prob=(1.0 - Model.current.dropout))


class Normalization(Unit):

    num_in = 1
    num_out = 1

    @staticmethod
    def valid(normalization):
        return normalization in ('instance', 'batch', 'global')

    def __init__(self, normalization, scale=True, offset=True, variance_epsilon=1e-6, name=None):
        super(Normalization, self).__init__(name=name)
        assert Normalization.valid(normalization)
        assert isinstance(scale, bool)
        assert isinstance(offset, bool)
        assert isinstance(variance_epsilon, float) and variance_epsilon > 0.0
        self.normalization = normalization
        self.scale = scale
        self.offset = offset
        self.variance_epsilon = variance_epsilon

    def initialize(self, x):
        super(Normalization, self).initialize(x)
        if self.normalization != 'instance':
            self.exp_moving_average = tf.train.ExponentialMovingAverage(decay=0.9, num_updates=None)
        mean_shape = tuple(1 for _ in range(rank(x) - 1)) + (shape(x)[-1],)
        if self.scale:
            self.scale = Variable(name='scale', shape=mean_shape, init='zeros')
        else:
            self.scale = None
        if self.offset:
            self.offset = Variable(name='offset', shape=mean_shape, init='zeros')
        else:
            self.offset = None

    def forward(self, x):
        super(Normalization, self).forward(x)
        if self.normalization == 'instance':
            mean, variance = tf.nn.moments(x=x, axes=tuple(range(1, rank(x))), keep_dims=True)
        elif self.normalization == 'batch':
            mean, variance = tf.nn.moments(x=x, axes=(0,), keep_dims=True)
        elif self.normalization == 'global':
            mean, variance = tf.nn.moments(x=x, axes=tuple(range(rank(x) - 1)), keep_dims=True)

        if self.normalization != 'instance':

            def true_fn():
                exp_moving_average_op = self.exp_moving_average.apply(var_list=(mean, variance))
                with tf.control_dependencies(control_inputs=(exp_moving_average_op,)):
                    return tf.identity(input=mean), tf.identity(input=variance)

            def false_fn():
                return self.exp_moving_average.average(var=mean), self.exp_moving_average.average(var=variance)

            mean, variance = tf.cond(pred=Model.current.training, true_fn=true_fn, false_fn=false_fn)

        if self.scale is None:
            scale = None
        else:
            scale = 1.0 + self.scale()
        if self.offset is None:
            offset = None
        else:
            offset = self.offset()
        return tf.nn.batch_normalization(x=x, mean=mean, variance=variance, offset=offset, scale=scale, variance_epsilon=self.variance_epsilon)


class FeaturewiseLinearModulation(Unit):

    num_in = 2
    num_out = 1

    def __init__(self, scale=Linear, offset=Linear, name=None):
        super(FeaturewiseLinearModulation, self).__init__(name=name)
        assert issubclass(scale, Layer)
        assert issubclass(offset, Layer)
        self.scale = scale
        self.offset = offset

    def initialize(self, x, condition):
        super(FeaturewiseLinearModulation, self).initialize(x, condition)
        size = shape(x)[-1]
        self.scale = self.scale(size=size)
        self.offset = self.offset(size=size)

    def forward(self, x, condition):
        super(FeaturewiseLinearModulation, self).forward(x, condition)
        scale = 1.0 + (condition >> self.scale)
        scale = tf.expand_dims(input=tf.expand_dims(input=scale, axis=1), axis=2)
        offset = condition >> self.offset
        offset = tf.expand_dims(input=tf.expand_dims(input=offset, axis=1), axis=2)
        return x * scale + offset


class FiLM(Unit):

    num_in = 2
    num_out = 1

    def __init__(self, layer, scale=Linear, offset=Linear, normalization='instance', activation='relu', dropout=False, norm_act_film_before=False, name=None, **kwargs):
        super(FiLM, self).__init__(name=name)
        assert issubclass(layer, Layer)
        assert issubclass(scale, Layer) and issubclass(offset, Layer)
        assert not normalization or Normalization.valid(normalization)
        assert not activation or Activation.valid(activation)
        assert isinstance(dropout, bool)
        assert isinstance(norm_act_film_before, bool)
        self.layer = layer
        self.scale = scale
        self.offset = offset
        self.normalization = normalization
        self.activation = activation
        self.dropout = dropout
        self.norm_act_film_before = norm_act_film_before
        self.kwargs = kwargs  # kwargs ??????????

    def initialize(self, x, condition):
        super(FiLM, self).initialize(x, condition)
        self.layer = self.layer(normalization=False, activation=None, dropout=False, **self.kwargs)
        self.film = FeaturewiseLinearModulation(offset=self.offset, scale=self.scale)
        self.normalization = Normalization(normalization=self.normalization, scale=False, offset=False) if self.normalization else None
        self.activation = Activation(activation=self.activation) if self.activation else None
        self.dropout = Dropout() if self.dropout else None

    def forward(self, x, condition):
        super(FiLM, self).forward(x, condition)
        if self.norm_act_film_before:
            if self.normalization is not None:
                x >>= self.normalization
            x = (x, condition) >> self.film
            if self.activation is not None:
                x >>= self.activation
            if self.dropout is not None:
                x >>= self.dropout
        x >>= self.layer
        if not self.norm_act_film_before:
            if self.normalization is not None:
                x >>= self.normalization
            x = (x, condition) >> self.film
            if self.activation is not None:
                x >>= self.activation
            if self.dropout is not None:
                x >>= self.dropout
        return x


class Reduction(Unit):

    num_in = -1
    num_out = 1

    @staticmethod
    def valid(reduction):
        return reduction in ('cbp', 'collapse', 'concat', 'conv', 'conv2d', 'last', 'max', 'mean', 'min', 'prod', 'stack', 'sum')

    def __init__(self, reduction, axis=-1, arg=-1, name=None):
        super(Reduction, self).__init__(name=name)
        assert Reduction.valid(reduction)
        if isinstance(axis, int):
            axis = (axis,)
        elif len(axis) == 3 and axis[1] is Ellipsis:
            assert isinstance(axis[0], int) and isinstance(axis[2], int)
            axis = tuple(axis)
        else:
            assert len(axis) > 0 and all(isinstance(a, int) for a in axis)
            axis = tuple(sorted(axis))
        assert len(set(axis)) == len(axis)
        assert isinstance(arg, int)
        self.reduction = reduction
        self.axis = axis
        self.arg = arg
        self.multiple_inputs = None
        self.weights = None

    def initialize(self, *xs):
        super(Reduction, self).initialize(*xs)
        if self.reduction in ('conv', 'conv2d'):
            self.weights = Variable(name='weights', init='in-out')

    def forward(self, *xs):
        super(Reduction, self).forward(*xs)
        assert len(xs) > 0
        if self.multiple_inputs is None:
            self.multiple_inputs = len(xs) > 1

        if self.multiple_inputs:
            assert self.axis == (-1,)
            assert all(rank(x) == rank(xs[0]) for x in xs)
            axis = self.axis[0] if self.axis[0] >= 0 else rank(xs[0]) + self.axis[0] + 1

            if self.reduction == 'max':
                y = xs[0]
                for x in xs[1:]:
                    y = tf.maximum(x=y, y=x)
                return y

            elif self.reduction == 'mean':
                y = xs[0]
                for x in xs[1:]:
                    y = tf.add(x=y, y=x)
                return y / float(len(xs))

            elif self.reduction == 'min':
                y = xs[0]
                for x in xs[1:]:
                    y = tf.minimum(x=y, y=x)
                return y

            elif self.reduction == 'prod':
                y = xs[0]
                for x in xs[1:]:
                    y = tf.multiply(x=y, y=x)
                return y

            elif self.reduction == 'sum':
                y = xs[0]
                for x in xs[1:]:
                    y = tf.add(x=y, y=x)
                return y

            elif self.reduction in ('collapse', 'conv', 'conv2d'):
                xs = make_least_common_shape(xs=xs)
                x = tf.stack(values=xs, axis=axis)

        else:
            x = xs[0]

            if len(self.axis) == 3 and self.axis[1] is Ellipsis:
                start, _, end = self.axis
                start = start if start >= 0 else rank(x) + start
                end = end if end >= 0 else rank(x) + end
                self.axis = tuple(range(start, end + 1))
            elif any(a < 0 for a in self.axis):
                self.axis = tuple(sorted(a if a >= 0 else rank(x) + a for a in self.axis))
                assert len(set(self.axis)) == len(self.axis)
            assert self.axis[0] >= 0 and self.axis[-1] < rank(x)

            if self.reduction in ('concat', 'stack'):
                for axis in reversed(self.axis):
                    xs = [y for x in xs for y in tf.unstack(value=x, axis=axis)]

        if self.reduction in ('concat', 'stack'):
            self.arg = self.arg if self.arg >= 0 else rank(xs[0]) + self.arg
            assert 0 <= self.arg < rank(xs[0])
            xs = make_least_common_shape(xs=xs, ignore_ranks=(self.arg,))

        if self.reduction == 'collapse':
            start = self.axis[0]
            end = self.axis[-1] + 1
            assert all(axis == n for n, axis in enumerate(self.axis, start))
            reduced_shape = shape(x)
            reduced_shape = reduced_shape[:start] + (product(reduced_shape[start:end]),) + reduced_shape[end:]
            return tf.reshape(tensor=x, shape=reduced_shape)

        elif self.reduction == 'concat':
            return tf.concat(values=xs, axis=self.arg)

        elif self.reduction == 'conv':
            assert self.axis == (-1,) or (len(self.axis) == rank(x) - 2 and all(axis == n for n, axis in enumerate(self.axis, 1)))
            reduced_shape = shape(x)
            reduced_shape = (reduced_shape[:1], product(reduced_shape[1:-1]), reduced_shape[-1:])
            x = tf.transpose(a=tf.reshape(tensor=x, shape=reduced_shape), perm=(0, 2, 1))
            self.weights.specify_shape(shape=(1, shape(x)[-1], 1))
            x = tf.nn.conv1d(value=x, filters=self.weights(), stride=1, padding='SAME')
            return tf.squeeze(input=x, axis=2)

        elif self.reduction == 'conv2d':
            assert self.axis == (-1,) or self.axis == (1, 2)
            self.weights.specify_shape(shape=(shape(x)[1], shape(x)[2], shape(x)[-1], shape(x)[-1]))
            x = tf.nn.conv2d(input=x, filter=self.weights(), strides=(1, 1, 1, 1), padding='VALID')
            return tf.squeeze(input=tf.squeeze(input=x, axis=2), axis=1)

        elif self.reduction == 'last':
            if self.multiple_inputs:
                return xs[-1]
            else:
                for axis in reversed(self.axis):
                    if axis == 0:
                        x = x[-1, Ellipsis]
                    elif axis == 1:
                        x = x[:, -1, Ellipsis]
                    elif axis == 2:
                        x = x[:, :, -1, Ellipsis]
                    elif axis == 3:
                        x = x[:, :, :, -1, Ellipsis]
                    elif axis == 4:
                        x = x[:, :, :, :, -1, Ellipsis]
                    else:
                        assert False
                return x

        elif self.reduction == 'max':
            return tf.reduce_max(input_tensor=x, axis=self.axis)

        elif self.reduction == 'mean':
            return tf.reduce_mean(input_tensor=x, axis=self.axis)

        elif self.reduction == 'min':
            return tf.reduce_min(input_tensor=x, axis=self.axis)

        elif self.reduction == 'prod':
            return tf.reduce_prod(input_tensor=x, axis=self.axis)

        elif self.reduction == 'stack':
            return tf.stack(values=xs, axis=self.arg)

        elif self.reduction == 'sum':
            return tf.reduce_sum(input_tensor=x, axis=self.axis)


# class Concatenation(Unit):

#     def __init__(self, axis=-1, name=None):
#         assert isinstance(axis, int)
#         self.axis = axis
#         super(Concatenation, self).__init__(name=name)

#     def forward(self, *xs):
#         assert len(xs) >= 1 and all(rank(x) == rank(xs[0]) for x in xs)  # what if length 0?
#         axis = self.axis if self.axis >= 0 else rank(xs[0]) + self.axis
#         assert rank(xs[0]) > axis
#         return tf.concat(values=xs, axis=axis)


# class Stack(Unit):

#     def __init__(self, axis=-1, name=None):
#         assert isinstance(axis, int)
#         self.axis = axis
#         super(Stack, self).__init__(name=name)

#     def forward(self, *xs):
#         assert len(xs) >= 1 and all(rank(x) == rank(xs[0]) for x in xs)
#         axis = self.axis if self.axis >= 0 else rank(xs[0]) + self.axis + 1
#         assert rank(xs[0]) >= axis
#         return tf.stack(values=xs, axis=axis)


class Attention(Unit):

    num_in = 2
    num_out = 1

    def __init__(self, assessment, name=None):
        super(Attention, self).__init__(name=name)
        assert isinstance(assessment, Unit)
        self.assessment = assessment
        self.softmax = None
        self.reduction = None

    def initialize(self, x, query):
        super(Attention, self).initialize(x, query)
        self.softmax = Activation(activation='softmax')
        self.reduction = Reduction(reduction='sum', axis=(1, Ellipsis, -2))

    def forward(self, x, query):
        super(Attention, self).forward(x, query)
        assert rank(x) > 2 and rank(query) == 2 and shape(query)[0] == shape(x)[0]
        for _ in range(rank(x) - 2):
            query = tf.expand_dims(input=query, axis=1)
        attention = (x, query) >> self.assessment >> self.softmax
        assert shape(attention) == shape(x)[:-1]
        attention = tf.expand_dims(input=attention, axis=(rank(x) - 1))
        return (x * attention) >> self.reduction


class CompactBilinearPooling(Unit):

    num_in = -1
    num_out = 1

    def __init__(self, size=None, name=None):
        super(CompactBilinearPooling, self).__init__(name=name)
        # assert GPU !!!
        # default arg size
        assert size is None or (isinstance(size, int) and size > 0)
        self.size = size
        self.sketch_indices = None
        self.sketch_values = None

    def initialize(self, *xs):
        super(CompactBilinearPooling, self).initialize(*xs)
        self.sketch_indices = Variable(name=('indices' + str(n)), shape=input_size, init='constant', value=sketch_indices)
        self.sketch_values = Variable(name=('values' + str(n)), shape=input_size, init='constant', value=sketch_values)

    def forward(self, *xs):
        super(CompactBilinearPooling, self).forward(*xs)
        assert len(xs) >= 1 and all(rank(x) == rank(xs[0]) for x in xs)
        # what if length 0?
        size = shape(xs[0])[-1] if self.size is None else self.size
        p = None
        for n, x in enumerate(xs):
            input_size = shape(x)[-1]
            indices = tf.range(start=input_size, dtype=tf.int64)
            indices = tf.expand_dims(input=indices, axis=1)
            sketch_indices = tf.random_uniform(shape=(input_size,), maxval=size, dtype=tf.int64)
            sketch_indices = tf.expand_dims(input=self.sketch_indices(), axis=1)
            sketch_indices = tf.concat(values=(indices, sketch_indices), axis=1)
            sketch_values = tf.random_uniform(shape=(input_size,))
            sketch_values = tf.round(x=self.sketch_values())
            sketch_values = sketch_values * 2 - 1
            sketch_matrix = tf.SparseTensor(indices=sketch_indices, values=sketch_values, dense_shape=(input_size, size))
            sketch_matrix = tf.sparse_reorder(sp_input=sketch_matrix)
            x = tf.sparse_tensor_dense_matmul(sp_a=sketch_matrix, b=x, adjoint_a=True, adjoint_b=True)
            x = tf.transpose(a=x)
            x = tf.reshape(tensor=x, shape=(shape(x)[0] or -1, size))
            x = tf.complex(real=x, imag=0.0)
            x = tf.fft(input=x)
            if p is None:
                p = x
            else:
                x, p = make_broadcastable(xs=(x, p))
                p = tf.multiply(x=p, y=x)
        return tf.ifft(input=tf.real(input=p))


class Pooling(Unit):

    num_in = 1
    num_out = 1

    @staticmethod
    def valid(pool):
        return pool in ('none', 'average', 'avg', 'max', 'maximum')

    def __init__(self, pool='max', window=(2, 2), stride=2, padding='SAME', name=None):
        super(Pooling, self).__init__(name=name)
        window = tuple(window)
        assert Pooling.valid(pool)
        assert len(window) == 2 and all(isinstance(n, int) and n > 0 for n in window)
        assert padding in ('SAME', 'VALID')
        self.pool = pool
        self.window = (1, window[0], window[1], 1)
        self.padding = padding
        if isinstance(stride, int):
            assert stride > 0
            self.stride = stride if len(window) == 1 else (1, stride, stride, 1)
        else:
            assert len(stride) == 2 and stride[0] > 0 and stride[1] > 0
            self.stride = (1, stride[0], stride[1], 1)

    def forward(self, x):
        super(Pooling, self).forward(x)
        if self.pool == 'none':
            return x
        elif self.pool in ('avg', 'average'):
            return tf.nn.avg_pool(value=x, ksize=self.window, strides=self.stride, padding=self.padding)
        elif self.pool in ('max', 'maximum'):
            return tf.nn.max_pool(value=x, ksize=self.window, strides=self.stride, padding=self.padding)


    # def unpool(self, x, unpooling_type='zero'):  # zero, id
    #     assert NeuralNetwork.rank(x) == 4 and NeuralNetwork.shape(x)[0] is None
    #     width, height, size = NeuralNetwork.shape(x)[1:]
    #     with tf.name_scope('unpool'):
    #         if unpooling_type == 'zero':
    #             zeros = tf.zeros_like(tensor=x)
    #             x = tf.stack(values=(x, zeros, zeros, zeros), axis=4)
    #             x = tf.reshape(tensor=x, shape=(-1, width, height, size, 2, 2))
    #             # x = tf.Print(x, (x[0,0,0,:,0,0],x[0,0,0,:,0,1],x[0,0,0,:,1,0],x[0,0,0,:,1,1]))
    #             x = tf.transpose(a=x, perm=(0, 1, 5, 2, 4, 3))
    #         elif unpooling_type == 'id':
    #             # x = tf.stack(values=(x, x, x, x), axis=4)
    #             x = tf.tile(input=x, multiples=(1, 1, 1, 4))
    #             x = tf.reshape(tensor=x, shape=(-1, width, height, 2, 2, size))
    #             # x = tf.Print(x, (x[0,0,0,0,0,:],x[0,0,0,0,1,:],x[0,0,0,1,0,:],x[0,0,0,1,1,:]))
    #             x = tf.transpose(a=x, perm=(0, 1, 4, 2, 3, 5))
    #         x = tf.reshape(tensor=x, shape=(-1, width * 2, height * 2, size))
    #     # x = tf.Print(x, (tf.reduce_all([tf.reduce_all([x[n,2*w,2*h,:] == y[n,w,h,:], x[n,2*w,2*h+1,:] == 0, x[n,2*w+1,2*h,:] == 0, x[n,2*w+1,2*h+1,:] == 0]) for n in range(128) for w in range(width) for h in range(height)]),))
    #     # x = tf.Print(x, (x[0,0,0,:],x[0,0,1,:],x[0,1,0,:],x[0,1,1,:]))
    #     # x = tf.Print(x, (x[0,2,0,:],x[0,2,1,:],x[0,3,0,:],x[0,3,1,:]))
    #     # x = tf.Print(x, (x[0,-2,-2,:],x[0,-2,-1,:],x[0,-1,-2,:],x[0,-1,-1,:]))
    #     assert NeuralNetwork.shape(x) == [None, width * 2, height * 2, size]
    #     return x


class Embedding(Unit):

    num_in = 1
    num_out = 1

    def __init__(self, indices, size, name=None):
        super(Embedding, self).__init__(name=name)
        assert isinstance(indices, int) and indices > 0
        assert isinstance(size, int) and size > 0
        self.indices = indices
        self.size = size
        self.embeddings = None

    def initialize(self, x):
        super(Embedding, self).initialize(x)
        self.embeddings = Variable(name='embeddings', shape=(self.indices, self.size))

    def forward(self, x):
        super(Embedding, self).forward(x)
        return tf.nn.embedding_lookup(params=self.embeddings(), ids=x)


class Split(Unit):

    num_in = 1
    num_out = -1

    def __init__(self, axis=1, size=1, reduction=None, name=None):
        super(Split, self).__init__(name=name)
        axis = (axis,) if isinstance(axis, int) else tuple(sorted(axis, reverse=True))
        size = (size,) if isinstance(size, int) else tuple(size)
        assert all(isinstance(a, int) and a >= 0 for a in axis)
        assert all(isinstance(s, int) and s > 0 for s in size)
        self.axis = axis
        self.size = size
        self.reduction = reduction

    def initialize(self, x):
        super(Split, self).initialize(x)
        self.reduction = None if self.reduction is None else Reduction(reduction=self.reduction)

    def forward(self, x):
        super(Split, self).forward(x)
        xs = [x]
        for a in self.axis:
            xs = [y for x in xs for y in tf.unstack(value=x, axis=a)]
        if self.size != (1,):
            xs = chain(*(combinations(xs, r=s) for s in self.size))  # others interesting? permutation, product?
        if self.reduction is not None:
            xs = [x >> self.reduction for x in xs]
        return tuple(xs)


class Relational(Unit):

    num_in = 2
    num_out = 1

    def __init__(self, relation_unit, axis=1, relation_reduction='concat', reduction='sum', name=None):
        super(Relational, self).__init__(name=name)
        self.relation_unit = relation_unit
        self.axis = axis
        self.relation_reduction = relation_reduction
        self.split = None
        self.reduction = reduction

    def initialize(self, x, y):
        super(Relational, self).initialize(x, y)
        self.split = Split(axis=self.axis, size=2, reduction=self.relation_reduction)
        self.reduction = Reduction(reduction=self.reduction)

    def forward(self, x, y):
        super(Relational, self).forward(x, y)
        xs = x >> self.split
        xs = [(x, y) >> Reduction(reduction='concat') >> self.relation_unit for x in xs]
        return xs >> self.reduction


class Index(Unit):

    num_in = 1
    num_out = 1

    def __init__(self, name=None):
        super(Index, self).__init__(name=name)

    def forward(self, x):
        super(Index, self).forward(x)
        index = None
        indexed_shape = shape(x)[1:-1]
        for n, dims in enumerate(indexed_shape):
            delta = 2.0 / (dims - 1)
            next_index = tf.range(start=-1.0, limit=(1.0 + 0.5 * delta), delta=delta, dtype=Model.dtype('float'))
            next_index = tf.expand_dims(input=next_index, axis=1)
            if index is None:
                index = next_index
            else:
                index = tf.stack(values=[index for _ in range(dims)], axis=n)
                for k, prev_dims in enumerate(indexed_shape[:n]):
                    next_index = tf.stack(values=[next_index for _ in range(prev_dims)], axis=k)
                index = tf.concat(values=(index, next_index), axis=(n + 1))
        index = tf.expand_dims(input=index, axis=0)
        multiples = [tf.shape(input=x)[0]] + [1] * (rank(x) - 1)
        index = tf.tile(input=index, multiples=multiples)
        return tf.concat(values=(x, index), axis=(rank(x) - 1))


class Dense(Layer):

    def __init__(self, size, bias=True, normalization='instance', activation='tanh', dropout=False, gated=False, norm_act_drop_before=False, name=None):
        super(Dense, self).__init__(size=size, name=name)
        assert isinstance(bias, bool)
        assert not normalization or Normalization.valid(normalization)
        assert not activation or Activation.valid(activation)
        assert isinstance(gated, bool)
        assert isinstance(dropout, bool)
        assert isinstance(norm_act_drop_before, bool)
        self.weights = None
        self.bias = bias
        self.normalization = normalization
        self.activation = activation
        self.dropout = dropout
        self.gated = gated
        self.norm_act_drop_before = norm_act_drop_before

    def initialize(self, x):
        super(Dense, self).initialize(x)
        self.weights = Variable(name='weights', init=(self.activation or 'in-out'))
        self.bias = Variable(name='bias', shape=self.size, init='zeros') if self.bias else None
        self.normalization = Normalization(normalization=self.normalization) if self.normalization else None
        self.activation = Activation(activation=self.activation) if self.activation else None
        self.dropout = Dropout() if self.dropout else None
        if self.gated:
            self.gate_weights = Variable(name='weights', init='sigmoid')
            self.gate_bias = Variable(name='bias', shape=self.size, init='zeros') if self.bias is not None else None
            self.gate_activation = Activation(activation='sigmoid')

    def forward(self, x):
        super(Dense, self).forward(x)
        assert 2 <= rank(x) <= 4
        if self.norm_act_drop_before:
            if self.normalization is not None:
                x >>= self.normalization
            if self.activation is not None:
                x >>= self.activation
            if self.dropout is not None:
                x >>= self.dropout
        if rank(x) == 2:
            self.weights.specify_shape(shape=(shape(x)[-1], self.size))
            x = tf.matmul(a=x, b=self.weights())
            if self.gated:
                self.gate_weights.specify_shape(shape=(shape(x)[-1], self.size))
                gate = tf.matmul(a=x, b=self.gate_weights())
        elif rank(x) == 3:
            self.weights.specify_shape(shape=(1, shape(x)[-1], self.size))
            x = tf.nn.conv1d(value=x, filters=self.weights(), stride=1, padding='SAME')
            if self.gated:
                self.gate_weights.specify_shape(shape=(1, shape(x)[-1], self.size))
                gate = tf.nn.conv1d(value=x, filters=self.gate_weights(), stride=1, padding='SAME')
        elif rank(x) == 4:
            self.weights.specify_shape(shape=(1, 1, shape(x)[-1], self.size))
            x = tf.nn.conv2d(input=x, filter=self.weights(), strides=(1, 1, 1, 1), padding='SAME')
            if self.gated:
                self.gate_weights.specify_shape(shape=(1, 1, shape(x)[-1], self.size))
                gate = tf.nn.conv2d(input=x, filter=self.gate_weights(), strides=(1, 1, 1, 1), padding='SAME')
        if self.bias is not None:
            x = tf.nn.bias_add(value=x, bias=self.bias())
            if self.gated:
                gate = tf.nn.bias_add(value=gate, bias=self.gate_bias())
        if self.squeeze:
            x = tf.squeeze(input=x, axis=-1)
            if self.gated:
                gate = tf.squeeze(input=gate, axis=-1)
        if not self.norm_act_drop_before:
            if self.normalization is not None:
                x >>= self.normalization
            if self.activation is not None:
                x >>= self.activation
            if self.dropout is not None:
                x >>= self.dropout
        if self.gated:
            x *= (gate >> self.gate_activation)
        return x


class Convolution(Layer):

    num_in = 1
    num_out = 1

    def __init__(self, size, index=False, window=(3, 3), stride=1, padding='SAME', transposed=False, bias=True, normalization='instance', activation='relu', dropout=False, norm_act_drop_before=False, name=None):  # gated???????????????????????????????????????????????????????????
        super(Convolution, self).__init__(size=size, name=name)
        window = (window,) if isinstance(window, int) else tuple(window)
        if isinstance(stride, int):
            stride = (stride,) if len(window) == 1 else (stride, stride)
        else:
            stride = (stride[0], stride[1])
        assert isinstance(index, bool)
        assert 1 <= len(window) <= 2 and all(isinstance(n, int) and n > 0 for n in window)
        assert len(stride) == len(window) and all(isinstance(n, int) and n > 0 for n in stride)
        assert padding in ('SAME', 'VALID')
        assert isinstance(transposed, bool) and (not transposed or len(window) == 2)
        assert isinstance(bias, bool)
        assert not normalization or Normalization.valid(normalization)
        assert not activation or Activation.valid(activation)
        assert isinstance(dropout, bool)
        assert isinstance(norm_act_drop_before, bool)
        self.index = index
        self.window = window
        self.stride = stride
        self.padding = padding
        self.transposed = transposed
        self.bias = bias
        self.normalization = normalization
        self.activation = activation
        self.dropout = dropout
        self.norm_act_drop_before = norm_act_drop_before

    def initialize(self, x):
        super(Convolution, self).initialize(x)
        if self.index:
            self.index = Index()
            input_size = shape(x)[-1] + rank(x) - 2
        else:
            self.index = None
            input_size = shape(x)[-1]
        if self.transposed:
            filters_shape = self.window + (self.size, input_size)
        else:
            filters_shape = self.window + (input_size, self.size)
        self.filters = Variable(name='filters', shape=filters_shape, init=(self.activation or 'in-out'))
        self.bias = Variable(name='bias', shape=(self.size,), init='zeros') if self.bias else None
        self.normalization = Normalization(normalization=self.normalization) if self.normalization else None
        self.activation = Activation(activation=self.activation) if self.activation else None
        self.dropout = Dropout() if self.dropout else None

    def forward(self, x):
        super(Convolution, self).forward(x)
        if self.norm_act_drop_before:
            if self.normalization is not None:
                x >>= self.normalization
            if self.activation is not None:
                x >>= self.activation
            if self.dropout is not None:
                x >>= self.dropout
        if self.index is not None:
            x >>= self.index
        if len(self.window) == 1:
            x = tf.nn.conv1d(value=x, filters=self.filters(), stride=self.stride[0], padding=self.padding)
        elif self.transposed:
            batch, height, width, _ = shape(x)
            x = tf.nn.conv2d_transpose(value=x, filter=self.filters(), output_shape=(batch, height * self.stride[1], width * self.stride[2], self.size), strides=((1,) + self.stride + (1,)), padding=self.padding)
        else:
            x = tf.nn.conv2d(input=x, filter=self.filters(), strides=((1,) + self.stride + (1,)), padding=self.padding)
        if self.bias is not None:
            x = tf.nn.bias_add(value=x, bias=self.bias())
        if self.squeeze:
            x = tf.squeeze(input=x, axis=-1)
        if not self.norm_act_drop_before:
            if self.normalization is not None:
                x >>= self.normalization
            if self.activation is not None:
                x >>= self.activation
            if self.dropout is not None:
                x >>= self.dropout
        return x


# class ConditionedConvolution(Unit):

#     num_in = 2
#     num_out = 1

#     def __init__(self, size, index=False, window=(3, 3), stride=1, padding='SAME', transposed=False, bias=True, normalization=True, activation='relu', dropout=False, norm_act_drop_before=False, name=None):  # gated???????????????????????????????????????????????????????????
#         super(ConditionedConvolution, self).__init__(name=name)
#         assert isinstance(size, int) and size >= 0
#         window = (window,) if isinstance(window, int) else tuple(window)
#         if isinstance(stride, int):
#             stride = (stride,) if len(window) == 1 else (stride, stride)
#         else:
#             stride = (stride[0], stride[1])
#         assert isinstance(index, bool)
#         assert 1 <= len(window) <= 2 and all(isinstance(n, int) and n > 0 for n in window)
#         assert len(stride) == len(window) and all(isinstance(n, int) and n > 0 for n in stride)
#         assert padding in ('SAME', 'VALID')
#         assert isinstance(transposed, bool) and (not transposed or len(window) == 2)
#         assert isinstance(bias, bool)
#         assert isinstance(normalization, bool) or isinstance(normalization, tuple)
#         assert activation is None or Activation.valid(activation)
#         assert isinstance(dropout, bool)
#         assert isinstance(norm_act_drop_before, bool)
#         if size == 0:
#             size = 1
#             self.squeeze = True
#         else:
#             self.squeeze = False
#         self.index = index
#         self.window = window
#         self.stride = stride
#         self.padding = padding
#         self.transposed = transposed
#         self.bias = bias
#         self.normalization = normalization
#         self.activation = activation
#         self.dropout = dropout
#         self.norm_act_drop_before = norm_act_drop_before

#     def initialize(self, x, condition=None):
#         super(ConditionedConvolution, self).initialize(x, condition)
#         if self.index:
#             self.index = Index()
#             input_size = shape(x)[-1] + rank(x) - 2
#         else:
#             self.index = None
#             input_size = shape(x)[-1]
#         if self.transposed:
#             filters_shape = self.window + (self.size, input_size)
#         else:
#             filters_shape = self.window + (input_size, self.size)
#         self.filters = Variable(name='filters', shape=filters_shape, init=(self.activation or 'in-out'))
#         self.bias = Variable(name='bias', shape=(self.size,), init='zeros') if self.bias else None
#         if isinstance(self.normalization, tuple):
#             assert len(self.normalization) == 2
#             self.normalization = Normalization(offset=self.normalization[0], scale=self.normalization[1])
#             self.requires_condition = True
#         elif self.normalization:
#             self.normalization = Normalization()
#             self.requires_condition = False
#         else:
#             self.normalization = None
#         self.activation = Activation(activation=self.activation) if self.activation else None
#         self.dropout = Dropout() if self.dropout else None

#     def forward(self, x, condition=None):
#         super(ConditionedConvolution, self).forward(x, condition)
#         if self.norm_act_drop_before:
#             if self.normalization is not None:
#                 if self.requires_condition:
#                     x = (x, condition) >> self.normalization
#                 else:
#                     x >>= self.normalization
#             if self.activation is not None:
#                 x >>= self.activation
#             if self.dropout is not None:
#                 x >>= self.dropout
#         if self.index is not None:
#             x >>= self.index
#         if len(self.window) == 1:
#             x = tf.nn.conv1d(value=x, filters=self.filters(), stride=self.stride[0], padding=self.padding)
#         elif self.transposed:
#             batch, height, width, _ = shape(x)
#             x = tf.nn.conv2d_transpose(value=x, filter=self.filters(), output_shape=(batch, height * self.stride[1], width * self.stride[2], self.size), strides=((1,) + self.stride + (1,)), padding=self.padding)
#         else:
#             x = tf.nn.conv2d(input=x, filter=self.filters(), strides=((1,) + self.stride + (1,)), padding=self.padding)
#         if self.bias:
#             x = tf.nn.bias_add(value=x, bias=self.bias())
#         if self.squeeze:
#             x = tf.squeeze(input=x, axis=-1)
#         if not self.norm_act_drop_before:
#             if self.normalization is not None:
#                 if self.requires_condition:
#                     x = (x, condition) >> self.normalization
#                 else:
#                     x >>= self.normalization
#             if self.activation is not None:
#                 x >>= self.activation
#             if self.dropout is not None:
#                 x >>= self.dropout
#         # if self.requires_condition:
#         #     return x, condition
#         # else:
#         return x


class NgramConvolution(Layer):

    def __init__(self, size, ngrams=3, padding='VALID', name=None):
        super(NgramConvolution, self).__init__(size=size, name=name)
        self.convolutions = []
        for ngram in range(1, ngrams + 1):  # not start with 1
            convolution = Convolution(size=size, window=ngram, normalization=False, activation='relu', padding=padding)  # norm, act?
            self.convolutions.append(convolution)

    def forward(self, x):
        super(NgramConvolution, self).forward(x)
        embeddings = [x >> convolution for convolution in self.convolutions]
        if self.squeeze:
            embeddings = [tf.squeeze(input=embedding, axis=-1) for embedding in embeddings]
        # requires SAME
        # phrase_embeddings = tf.stack(values=embeddings, axis=1)
        # phrase_embeddings = tf.reduce_max(input_tensor=phrase_embeddings, axis=???)
        # two reductions, and both concat only possible with SAME
        # maybe lstm?
        return tf.concat(values=embeddings, axis=1)


class RnnCell(Layer):

    @staticmethod
    def valid(cell):
        return cell in ('gru', 'lstm', 'simple')

    @staticmethod
    def from_name(cell):
        if cell == 'gru':
            return Gru
        elif cell == 'lstm':
            return Lstm
        elif cell == 'simple':
            return SimpleRnn
        else:
            raise Exception()

    # variables not registered !!!

    # CellWrapper???
    #   def _rnn_get_variable(self, getter, *args, **kwargs):
    #     variable = getter(*args, **kwargs)
    #     if context.in_graph_mode():
    #       trainable = (variable in tf_variables.trainable_variables() or
    #                    (isinstance(variable, tf_variables.PartitionedVariable) and
    #                     list(variable)[0] in tf_variables.trainable_variables()))
    #     else:
    #       trainable = variable._trainable  # pylint: disable=protected-access
    #     if trainable and variable not in self._trainable_weights:
    #       self._trainable_weights.append(variable)
    #     elif not trainable and variable not in self._non_trainable_weights:
    #       self._non_trainable_weights.append(variable)
    #     return variable

    @classmethod
    def size_from_state_size(state_size):
        return state_size

    def __init__(self, size, initial_state_shape=None, initial_state_variable=False, name=None):
        super(RnnCell, self).__init__(size=size, name=name)
        assert not self.squeeze
        self.initial_state_shape = (1, self.size) if initial_state_shape is None else (1,) + initial_state_shape
        self.initial_state_variable = initial_state_variable

    def initialize(self, x, cell):
        super(RnnCell, self).initialize(x)
        self.cell = cell
        if self.initial_state_variable:
            self.initial_state = Variable(name='init', shape=self.initial_state_shape, dtype='float')
        else:
            self.initial_state = tf.zeros(shape=self.initial_state_shape, dtype=Model.dtype('float'))

    def get_cell(self):
        return self.cell

    def get_initial_state(self, batch_size):
        if self.initial_state_variable:
            initial_state = self.initial_state()
        else:
            initial_state = self.initial_state
        multiples = tuple(batch_size if n == 0 else 1 for n in range(len(self.initial_state_shape)))
        return tf.tile(input=initial_state, multiples=multiples)

    def get_final_state(self, state):
        return state

    def forward(self, x, state):
        super(RnnCell, self).forward(x, state)
        return self.lstm(inputs=x, state=state)


class SimpleRnn(RnnCell):

    def __init__(self, size, layer, initial_state_shape=None, initial_state_variable=False, name=None):
        super(SimpleRnn, self).__init__(size=size, name=name)
        # ?????
        # assert not self.squeeze
        # self.initial_state_shape = (1, self.size) if self.initial_state_shape is None else (1,) + self.initial_state_shape
        # self.initial_state_variable = initial_state_variable


class Lstm(RnnCell):

    num_in = 2
    num_out = 2

    @classmethod
    def size_from_state_size(state_size):
        assert state_size % 2 == 0
        return state_size // 2

    def __init__(self, size, initial_state_variable=False, name=None):
        super(Lstm, self).__init__(size=size, initial_state_shape=(2, size), initial_state_variable=initial_state_variable, name=name)

    def initialize(self, x):
        lstm = tf.contrib.rnn.LSTMCell(num_units=self.size)
        super(Lstm, self).initialize(x, lstm)

    def get_initial_state(self, batch_size):
        initial_state = super(Lstm, self).get_initial_state(batch_size=batch_size)
        return tf.contrib.rnn.LSTMStateTuple(*tf.unstack(value=initial_state, axis=1))

    def get_final_state(self, state):
        return tf.concat(values=(state.c, state.h), axis=1)


class Gru(RnnCell):

    num_in = 2
    num_out = 2

    def initialize(self, x):
        gru = tf.contrib.rnn.GRUCell(num_units=self.size)
        super(Gru, self).initialize(x, gru)


class Rnn(Layer):

    num_in = 2
    num_out = 2

    def __init__(self, size, state_size=None, cell='lstm', initial_state_variable=False, name=None):
        if RnnCell.valid(cell=cell):
            cell = RnnCell.from_name(cell=cell)
        if size is None:
            assert state_size is not None
            size = cell.size_from_state_size(state_size=state_size)
        super(Rnn, self).__init__(size=size, name=name)
        assert not self.squeeze
        assert issubclass(cell, RnnCell)
        assert isinstance(initial_state_variable, bool)
        self.cell = cell
        self.initial_state_variable = initial_state_variable

    def initialize(self, x, length=None):
        super(Rnn, self).initialize(x, length)
        self.cell = self.cell(size=self.size, initial_state_variable=self.initial_state_variable)
        self.cell.initialize(x=x)

    def forward(self, x, length=None):
        super(Rnn, self).forward(x, length)
        if length is not None and rank(length) == 2:
            length = tf.squeeze(input=length, axis=1)
        cell = self.cell.get_cell()
        batch_size = tf.shape(input=x)[0]
        initial_state = self.cell.get_initial_state(batch_size=batch_size)
        x, state = tf.nn.dynamic_rnn(cell=cell, inputs=x, sequence_length=length, initial_state=initial_state, dtype=Model.dtype('float'))
        state = self.cell.get_final_state(state=state)
        return x, state


# class Expand(Layer):

#     def __init__(self, size, bottleneck=False, unit=Convolution, name=None):
#         assert issubclass(unit, Layer)
#         self.bottleneck = unit(size=(size * bottleneck)) if bottleneck else Identity()
#         self.unit = unit(size=size)
#         super(Residual, self).__init__(size=size, name=name)

#     def forward(self, x):
#         fx = x >> self.bottleneck >> self.unit
#         return tf.concat(values=(x, fx), axis=(rank(x) - 1))


class Repeat(LayerStack):

    def __init__(self, layer, sizes, name=None, **kwargs):
        super(Repeat, self).__init__(name=name)
        assert issubclass(layer, Layer)
        self.num_in = layer.num_in
        self.num_out = layer.num_out
        self.layer = layer
        self.sizes = sizes
        self.kwargs = kwargs

    # def __str__(self):
    #     if issubclass(self.layer, Layer):
    #         return super(Repeat, self).__str__() + '-' + self.layer.__name__
    #     else:
    #         return super(Repeat, self).__str__()

    def initialize(self, *xs):
        super(Repeat, self).initialize(*xs)
        kwargs_list = [dict(size=size) for n, size in enumerate(self.sizes)]
        for name, value in self.kwargs.items():
            if isinstance(value, list):
                assert len(value) == len(kwargs_list)
                for n in range(len(value)):
                    kwargs_list[n][name] = value[n]
            else:
                for n in range(len(kwargs_list)):
                    kwargs_list[n][name] = value
        for kwargs in kwargs_list:
            self.layers.append(self.layer(**dict(kwargs)))


class ConvolutionalNet(LayerStack):

    def __init__(self, sizes, depths, pool='max', name=None):
        super(LayerStack, self).__init__(name=name)
        assert Pooling.valid(pool)
        self.sizes = sizes
        self.depths = depths
        self.pool = pool

    def initialize(self, x):
        super(ConvolutionalNet, self).initialize(x)
        for m, (size, depth) in enumerate(zip(self.sizes, self.depths)):
            if m > 0:
                self.layers.append(Pooling(pool=self.pool))
            for n in range(depth):
                self.layers.append(Convolution(size=size))


class Residual(Layer):

    def __init__(self, size, unit=Convolution, depth=2, transform=True, reduction='sum', name=None):
        super(Residual, self).__init__(size=size, name=name)
        assert isinstance(depth, int) and depth > 0
        assert not self.squeeze or depth == 1
        assert isinstance(transform, (bool, Layer))
        self.unit = unit
        self.depth = depth
        self.transform = transform
        self.reduction = reduction

    def initialize(self, x):
        super(Residual, self).initialize(x)
        self.units = list()
        for _ in range(self.depth):
            self.units.append(self.unit(size=self.size))
        if shape(x)[-1] == self.size:
            self.transform = None
        elif isinstance(self.transform, Layer):
            self.transform = self.transform(size=self.size)
        else:
            self.transform = self.unit(size=self.size)
        self.reduction = Reduction(reduction=self.reduction)

    def forward(self, x):
        super(Residual, self).forward(x)
        res = x
        for unit in self.units:
            res >>= unit
        if self.transform is not None:
            x >>= self.transform
        assert shape(x) == shape(res)
        return (x, res) >> self.reduction


class ResidualNet(LayerStack):

    # citation!

    def __init__(self, sizes, depths, layer=Convolution, transition=None, pool='max', name=None):
        super(LayerStack, self).__init__(name=name)
        assert Pooling.valid(pool)
        self.sizes = sizes
        self.depths = depths
        self.layer = layer
        self.transition = transition
        self.pool = pool

    def initialize(self, x):
        super(ResidualNet, self).initialize(x)
        for m, (size, depth) in enumerate(zip(self.sizes, self.depths)):
            if m > 0:
                self.layers.append(Pooling(pool=self.pool))
            # if transition:
            #     layers.append(transition(size=size, normalize=False, activation))
            for n in range(depth):
                if m == 0:
                    if n == 0:
                        self.layers.append(self.layer(size=size, normalization=False, activation=None))
                        layer = (lambda size: self.layer(size=size, norm_act_drop_before=True))  # pre activation
                else:
                    self.layers.append(Residual(size=size, unit=layer))
        self.layers.append(Normalization(normalization='instance'))
        self.layers.append(Activation(activation='relu'))


class Fractal(Layer):

    def __init__(self, size, unit=Convolution, depth=3, reduction='mean', name=None):
        super(Fractal, self).__init__(size=size, name=name)
        assert isinstance(depth, int) and depth >= 0
        assert not self.squeeze or depth == 0
        self.unit = unit
        self.depth = depth
        self.reduction = reduction

    def initialize(self, x):
        super(Fractal, self).initialize(x)
        if self.depth > 0:
            self.fx = Fractal(size=self.size, unit=self.unit, depth=(self.depth - 1))
            self.ffx = Fractal(size=self.size, unit=self.unit, depth=(self.depth - 1))
            self.reduction = Reduction(reduction=self.reduction)
        self.unit = self.unit(size=self.size)

    def forward(self, x):
        super(Fractal, self).forward(x)
        if self.depth == 0:
            return x >> self.unit
        y = x >> self.fx >> self.ffx
        x >>= self.unit
        return (x, y) >> self.reduction


class FractalNet(LayerStack):

    def __init__(self, sizes, layer=Convolution, pool='max', name=None):
        assert Pooling.valid(pool)
        self.sizes = sizes
        self.layer = layer
        self.pool = pool
        super(LayerStack, self).__init__(name=name)

    def initialize(self, x):
        super(FractalNet, self).initialize(x)
        for m, size in enumerate(self.sizes):
            if m > 0:
                self.layers.append(Pooling(pool=self.pool))
            # if self.transition:
            #     self.layers.append(transition(size=size, normalize=False, activation))
            self.layers.append(Fractal(size=size, unit=self.layer))


FullyConnected = Full = FC = Dense
