# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# coding: utf-8
# pylint: disable=no-member, invalid-name, protected-access, no-self-use
# pylint: disable=too-many-branches, too-many-arguments, no-self-use
# pylint: disable=too-many-lines, arguments-differ
"""Definition of various recurrent neural network cells."""
from __future__ import print_function

from ... import symbol, ndarray
from ...base import string_types, numeric_types, _as_list
from ..block import Block, HybridBlock
from ..utils import _indent
from .. import tensor_types


def _cells_state_info(cells, batch_size):
    return sum([c.state_info(batch_size) for c in cells], [])

def _cells_begin_state(cells, **kwargs):
    return sum([c.begin_state(**kwargs) for c in cells], [])

def _get_begin_state(cell, F, begin_state, inputs, batch_size):
    if begin_state is None:
        if F is ndarray:
            ctx = inputs.context if isinstance(inputs, tensor_types) else inputs[0].context
            with ctx:
                begin_state = cell.begin_state(func=F.zeros, batch_size=batch_size)
        else:
            begin_state = cell.begin_state(func=F.zeros, batch_size=batch_size)
    return begin_state

def _format_sequence(length, inputs, layout, merge, in_layout=None):
    assert inputs is not None, \
        "unroll(inputs=None) has been deprecated. " \
        "Please create input variables outside unroll."

    axis = layout.find('T')
    batch_axis = layout.find('N')
    batch_size = 0
    in_axis = in_layout.find('T') if in_layout is not None else axis
    if isinstance(inputs, symbol.Symbol):
        F = symbol
        if merge is False:
            assert len(inputs.list_outputs()) == 1, \
                "unroll doesn't allow grouped symbol as input. Please convert " \
                "to list with list(inputs) first or let unroll handle splitting."
            inputs = list(symbol.split(inputs, axis=in_axis, num_outputs=length,
                                       squeeze_axis=1))
    elif isinstance(inputs, ndarray.NDArray):
        F = ndarray
        batch_size = inputs.shape[batch_axis]
        if merge is False:
            assert length is None or length == inputs.shape[in_axis]
            inputs = _as_list(ndarray.split(inputs, axis=in_axis,
                                            num_outputs=inputs.shape[in_axis],
                                            squeeze_axis=1))
    else:
        assert length is None or len(inputs) == length
        if isinstance(inputs[0], symbol.Symbol):
            F = symbol
        else:
            F = ndarray
            batch_size = inputs[0].shape[batch_axis]
        if merge is True:
            inputs = [F.expand_dims(i, axis=axis) for i in inputs]
            inputs = F.concat(*inputs, dim=axis)
            in_axis = axis

    if isinstance(inputs, tensor_types) and axis != in_axis:
        inputs = F.swapaxes(inputs, dim1=axis, dim2=in_axis)

    return inputs, axis, F, batch_size


class RecurrentCell(Block):
    """Abstract base class for RNN cells

    Parameters
    ----------
    prefix : str, optional
        Prefix for names of `Block`s
        (this prefix is also used for names of weights if `params` is `None`
        i.e. if `params` are being created and not reused)
    params : Parameter or None, optional
        Container for weight sharing between cells.
        A new Parameter container is created if `params` is `None`.
    """
    def __init__(self, prefix=None, params=None):
        super(RecurrentCell, self).__init__(prefix=prefix, params=params)
        self._modified = False
        self.reset()

    def __repr__(self):
        s = '{name}({mapping}'
        if hasattr(self, '_activation'):
            s += ', {_activation}'
        s += ')'
        mapping = ('{_input_size} -> {_hidden_size}'.format(**self.__dict__) if self._input_size
                   else self._hidden_size)
        return s.format(name=self.__class__.__name__,
                        mapping=mapping,
                        **self.__dict__)

    def reset(self):
        """Reset before re-using the cell for another graph."""
        self._init_counter = -1
        self._counter = -1

    def state_info(self, batch_size=0):
        """shape and layout information of states"""
        raise NotImplementedError()

    def begin_state(self, batch_size=0, func=ndarray.zeros, **kwargs):
        """Initial state for this cell.

        Parameters
        ----------
        func : callable, default symbol.zeros
            Function for creating initial state.

            For Symbol API, func can be `symbol.zeros`, `symbol.uniform`,
            `symbol.var etc`. Use `symbol.var` if you want to directly
            feed input as states.

            For NDArray API, func can be `ndarray.zeros`, `ndarray.ones`, etc.
        batch_size: int, default 0
            Only required for NDArray API. Size of the batch ('N' in layout)
            dimension of input.

        **kwargs :
            Additional keyword arguments passed to func. For example
            `mean`, `std`, `dtype`, etc.

        Returns
        -------
        states : nested list of Symbol
            Starting states for the first RNN step.
        """
        assert not self._modified, \
            "After applying modifier cells (e.g. ZoneoutCell) the base " \
            "cell cannot be called directly. Call the modifier cell instead."
        states = []
        for info in self.state_info(batch_size):
            self._init_counter += 1
            if info is not None:
                info.update(kwargs)
            else:
                info = kwargs
            state = func(name='%sbegin_state_%d'%(self._prefix, self._init_counter),
                         **info)
            states.append(state)
        return states

    def unroll(self, length, inputs, begin_state=None, layout='NTC', merge_outputs=None):
        """Unrolls an RNN cell across time steps.

        Parameters
        ----------
        length : int
            Number of steps to unroll.
        inputs : Symbol, list of Symbol, or None
            If `inputs` is a single Symbol (usually the output
            of Embedding symbol), it should have shape
            (batch_size, length, ...) if `layout` is 'NTC',
            or (length, batch_size, ...) if `layout` is 'TNC'.

            If `inputs` is a list of symbols (usually output of
            previous unroll), they should all have shape
            (batch_size, ...).
        begin_state : nested list of Symbol, optional
            Input states created by `begin_state()`
            or output state of another cell.
            Created from `begin_state()` if `None`.
        layout : str, optional
            `layout` of input symbol. Only used if inputs
            is a single Symbol.
        merge_outputs : bool, optional
            If `False`, returns outputs as a list of Symbols.
            If `True`, concatenates output across time steps
            and returns a single symbol with shape
            (batch_size, length, ...) if layout is 'NTC',
            or (length, batch_size, ...) if layout is 'TNC'.
            If `None`, output whatever is faster.

        Returns
        -------
        outputs : list of Symbol or Symbol
            Symbol (if `merge_outputs` is True) or list of Symbols
            (if `merge_outputs` is False) corresponding to the output from
            the RNN from this unrolling.

        states : list of Symbol
            The new state of this RNN after this unrolling.
            The type of this symbol is same as the output of `begin_state()`.
        """
        self.reset()

        inputs, _, F, batch_size = _format_sequence(length, inputs, layout, False)
        begin_state = _get_begin_state(self, F, begin_state, inputs, batch_size)

        states = begin_state
        outputs = []
        for i in range(length):
            output, states = self(inputs[i], states)
            outputs.append(output)

        outputs, _, _, _ = _format_sequence(length, outputs, layout, merge_outputs)

        return outputs, states

    #pylint: disable=no-self-use
    def _get_activation(self, F, inputs, activation, **kwargs):
        """Get activation function. Convert if is string"""
        if isinstance(activation, string_types):
            return F.Activation(inputs, act_type=activation, **kwargs)
        else:
            return activation(inputs, **kwargs)

    def forward(self, inputs, states):
        """Unrolls the recurrent cell for one time step.

        Parameters
        ----------
        inputs : sym.Variable
            Input symbol, 2D, of shape (batch_size * num_units).
        states : list of sym.Variable
            RNN state from previous step or the output of begin_state().

        Returns
        -------
        output : Symbol
            Symbol corresponding to the output from the RNN when unrolling
            for a single time step.
        states : list of Symbol
            The new state of this RNN after this unrolling.
            The type of this symbol is same as the output of `begin_state()`.
            This can be used as an input state to the next time step
            of this RNN.

        See Also
        --------
        begin_state: This function can provide the states for the first time step.
        unroll: This function unrolls an RNN for a given number of (>=1) time steps.
        """
        # pylint: disable= arguments-differ
        self._counter += 1
        return super(RecurrentCell, self).forward(inputs, states)


class HybridRecurrentCell(RecurrentCell, HybridBlock):
    """HybridRecurrentCell supports hybridize."""
    def __init__(self, prefix=None, params=None):
        super(HybridRecurrentCell, self).__init__(prefix=prefix, params=params)

    def hybrid_forward(self, F, x, *args, **kwargs):
        raise NotImplementedError


class RNNCell(HybridRecurrentCell):
    """Simple recurrent neural network cell.

    Parameters
    ----------
    hidden_size : int
        Number of units in output symbol
    activation : str or Symbol, default 'tanh'
        Type of activation function.
    i2h_weight_initializer : str or Initializer
        Initializer for the input weights matrix, used for the linear
        transformation of the inputs.
    h2h_weight_initializer : str or Initializer
        Initializer for the recurrent weights matrix, used for the linear
        transformation of the recurrent state.
    i2h_bias_initializer : str or Initializer
        Initializer for the bias vector.
    h2h_bias_initializer : str or Initializer
        Initializer for the bias vector.
    prefix : str, default 'rnn_'
        Prefix for name of `Block`s
        (and name of weight if params is `None`).
    params : Parameter or None
        Container for weight sharing between cells.
        Created if `None`.
    """
    def __init__(self, hidden_size, activation='tanh',
                 i2h_weight_initializer=None, h2h_weight_initializer=None,
                 i2h_bias_initializer='zeros', h2h_bias_initializer='zeros',
                 input_size=0, prefix=None, params=None):
        super(RNNCell, self).__init__(prefix=prefix, params=params)
        self._hidden_size = hidden_size
        self._activation = activation
        self._input_size = input_size
        self.i2h_weight = self.params.get('i2h_weight', shape=(hidden_size, input_size),
                                          init=i2h_weight_initializer,
                                          allow_deferred_init=True)
        self.h2h_weight = self.params.get('h2h_weight', shape=(hidden_size, hidden_size),
                                          init=h2h_weight_initializer,
                                          allow_deferred_init=True)
        self.i2h_bias = self.params.get('i2h_bias', shape=(hidden_size,),
                                        init=i2h_bias_initializer,
                                        allow_deferred_init=True)
        self.h2h_bias = self.params.get('h2h_bias', shape=(hidden_size,),
                                        init=h2h_bias_initializer,
                                        allow_deferred_init=True)

    def state_info(self, batch_size=0):
        return [{'shape': (batch_size, self._hidden_size), '__layout__': 'NC'}]

    def _alias(self):
        return 'rnn'

    def hybrid_forward(self, F, inputs, states, i2h_weight,
                       h2h_weight, i2h_bias, h2h_bias):
        prefix = 't%d_'%self._counter
        i2h = F.FullyConnected(data=inputs, weight=i2h_weight, bias=i2h_bias,
                               num_hidden=self._hidden_size,
                               name=prefix+'i2h')
        h2h = F.FullyConnected(data=states[0], weight=h2h_weight, bias=h2h_bias,
                               num_hidden=self._hidden_size,
                               name=prefix+'h2h')
        output = self._get_activation(F, i2h + h2h, self._activation,
                                      name=prefix+'out')

        return output, [output]


class LSTMCell(HybridRecurrentCell):
    """Long-Short Term Memory (LSTM) network cell.

    Parameters
    ----------
    hidden_size : int
        Number of units in output symbol.
    i2h_weight_initializer : str or Initializer
        Initializer for the input weights matrix, used for the linear
        transformation of the inputs.
    h2h_weight_initializer : str or Initializer
        Initializer for the recurrent weights matrix, used for the linear
        transformation of the recurrent state.
    i2h_bias_initializer : str or Initializer, default 'lstmbias'
        Initializer for the bias vector. By default, bias for the forget
        gate is initialized to 1 while all other biases are initialized
        to zero.
    h2h_bias_initializer : str or Initializer
        Initializer for the bias vector.
    prefix : str, default 'lstm_'
        Prefix for name of `Block`s
        (and name of weight if params is `None`).
    params : Parameter or None
        Container for weight sharing between cells.
        Created if `None`.
    """
    def __init__(self, hidden_size,
                 i2h_weight_initializer=None, h2h_weight_initializer=None,
                 i2h_bias_initializer='zeros', h2h_bias_initializer='zeros',
                 input_size=0, prefix=None, params=None):
        super(LSTMCell, self).__init__(prefix=prefix, params=params)

        self._hidden_size = hidden_size
        self._input_size = input_size
        self.i2h_weight = self.params.get('i2h_weight', shape=(4*hidden_size, input_size),
                                          init=i2h_weight_initializer,
                                          allow_deferred_init=True)
        self.h2h_weight = self.params.get('h2h_weight', shape=(4*hidden_size, hidden_size),
                                          init=h2h_weight_initializer,
                                          allow_deferred_init=True)
        self.i2h_bias = self.params.get('i2h_bias', shape=(4*hidden_size,),
                                        init=i2h_bias_initializer,
                                        allow_deferred_init=True)
        self.h2h_bias = self.params.get('h2h_bias', shape=(4*hidden_size,),
                                        init=h2h_bias_initializer,
                                        allow_deferred_init=True)

    def state_info(self, batch_size=0):
        return [{'shape': (batch_size, self._hidden_size), '__layout__': 'NC'},
                {'shape': (batch_size, self._hidden_size), '__layout__': 'NC'}]

    def _alias(self):
        return 'lstm'

    def hybrid_forward(self, F, inputs, states, i2h_weight,
                       h2h_weight, i2h_bias, h2h_bias):
        prefix = 't%d_'%self._counter
        i2h = F.FullyConnected(data=inputs, weight=i2h_weight, bias=i2h_bias,
                               num_hidden=self._hidden_size*4, name=prefix+'i2h')
        h2h = F.FullyConnected(data=states[0], weight=h2h_weight, bias=h2h_bias,
                               num_hidden=self._hidden_size*4, name=prefix+'h2h')
        gates = i2h + h2h
        slice_gates = F.SliceChannel(gates, num_outputs=4, name=prefix+'slice')
        in_gate = F.Activation(slice_gates[0], act_type="sigmoid", name=prefix+'i')
        forget_gate = F.Activation(slice_gates[1], act_type="sigmoid", name=prefix+'f')
        in_transform = F.Activation(slice_gates[2], act_type="tanh", name=prefix+'c')
        out_gate = F.Activation(slice_gates[3], act_type="sigmoid", name=prefix+'o')
        next_c = F._internal._plus(forget_gate * states[1], in_gate * in_transform,
                                   name=prefix+'state')
        next_h = F._internal._mul(out_gate, F.Activation(next_c, act_type="tanh"),
                                  name=prefix+'out')

        return next_h, [next_h, next_c]


class GRUCell(HybridRecurrentCell):
    """Gated Rectified Unit (GRU) network cell.
    Note: this is an implementation of the cuDNN version of GRUs
    (slight modification compared to Cho et al. 2014).

    Parameters
    ----------
    hidden_size : int
        Number of units in output symbol.
    i2h_weight_initializer : str or Initializer
        Initializer for the input weights matrix, used for the linear
        transformation of the inputs.
    h2h_weight_initializer : str or Initializer
        Initializer for the recurrent weights matrix, used for the linear
        transformation of the recurrent state.
    i2h_bias_initializer : str or Initializer
        Initializer for the bias vector.
    h2h_bias_initializer : str or Initializer
        Initializer for the bias vector.
    prefix : str, default 'gru_'
        prefix for name of `Block`s
        (and name of weight if params is `None`).
    params : Parameter or None
        Container for weight sharing between cells.
        Created if `None`.
    """
    def __init__(self, hidden_size,
                 i2h_weight_initializer=None, h2h_weight_initializer=None,
                 i2h_bias_initializer='zeros', h2h_bias_initializer='zeros',
                 input_size=0, prefix=None, params=None):
        super(GRUCell, self).__init__(prefix=prefix, params=params)
        self._hidden_size = hidden_size
        self._input_size = input_size
        self.i2h_weight = self.params.get('i2h_weight', shape=(3*hidden_size, input_size),
                                          init=i2h_weight_initializer,
                                          allow_deferred_init=True)
        self.h2h_weight = self.params.get('h2h_weight', shape=(3*hidden_size, hidden_size),
                                          init=h2h_weight_initializer,
                                          allow_deferred_init=True)
        self.i2h_bias = self.params.get('i2h_bias', shape=(3*hidden_size,),
                                        init=i2h_bias_initializer,
                                        allow_deferred_init=True)
        self.h2h_bias = self.params.get('h2h_bias', shape=(3*hidden_size,),
                                        init=h2h_bias_initializer,
                                        allow_deferred_init=True)

    def state_info(self, batch_size=0):
        return [{'shape': (batch_size, self._hidden_size), '__layout__': 'NC'}]

    def _alias(self):
        return 'gru'

    def hybrid_forward(self, F, inputs, states, i2h_weight,
                       h2h_weight, i2h_bias, h2h_bias):
        # pylint: disable=too-many-locals
        prefix = 't%d_'%self._counter
        prev_state_h = states[0]
        i2h = F.FullyConnected(data=inputs,
                               weight=i2h_weight,
                               bias=i2h_bias,
                               num_hidden=self._hidden_size * 3,
                               name=prefix+'i2h')
        h2h = F.FullyConnected(data=prev_state_h,
                               weight=h2h_weight,
                               bias=h2h_bias,
                               num_hidden=self._hidden_size * 3,
                               name=prefix+'h2h')

        i2h_r, i2h_z, i2h = F.SliceChannel(i2h, num_outputs=3,
                                           name=prefix+'i2h_slice')
        h2h_r, h2h_z, h2h = F.SliceChannel(h2h, num_outputs=3,
                                           name=prefix+'h2h_slice')

        reset_gate = F.Activation(i2h_r + h2h_r, act_type="sigmoid",
                                  name=prefix+'r_act')
        update_gate = F.Activation(i2h_z + h2h_z, act_type="sigmoid",
                                   name=prefix+'z_act')

        next_h_tmp = F.Activation(i2h + reset_gate * h2h, act_type="tanh",
                                  name=prefix+'h_act')

        next_h = F._internal._plus((1. - update_gate) * next_h_tmp, update_gate * prev_state_h,
                                   name=prefix+'out')

        return next_h, [next_h]


class SequentialRNNCell(RecurrentCell):
    """Sequentially stacking multiple RNN cells."""
    def __init__(self, prefix=None, params=None):
        super(SequentialRNNCell, self).__init__(prefix=prefix, params=params)

    def __repr__(self):
        s = '{name}(\n{modstr}\n)'
        return s.format(name=self.__class__.__name__,
                        modstr='\n'.join(['({i}): {m}'.format(i=i, m=_indent(m.__repr__(), 2))
                                          for i, m in enumerate(self._children)]))

    def add(self, cell):
        """Appends a cell into the stack.

        Parameters
        ----------
        cell : rnn cell
        """
        self.register_child(cell)

    def state_info(self, batch_size=0):
        return _cells_state_info(self._children, batch_size)

    def begin_state(self, **kwargs):
        assert not self._modified, \
            "After applying modifier cells (e.g. ZoneoutCell) the base " \
            "cell cannot be called directly. Call the modifier cell instead."
        return _cells_begin_state(self._children, **kwargs)

    def __call__(self, inputs, states):
        self._counter += 1
        next_states = []
        p = 0
        for cell in self._children:
            assert not isinstance(cell, BidirectionalCell)
            n = len(cell.state_info())
            state = states[p:p+n]
            p += n
            inputs, state = cell(inputs, state)
            next_states.append(state)
        return inputs, sum(next_states, [])

    def unroll(self, length, inputs, begin_state=None, layout='NTC', merge_outputs=None):
        self.reset()

        inputs, _, F, batch_size = _format_sequence(length, inputs, layout, None)
        num_cells = len(self._children)
        begin_state = _get_begin_state(self, F, begin_state, inputs, batch_size)

        p = 0
        next_states = []
        for i, cell in enumerate(self._children):
            n = len(cell.state_info())
            states = begin_state[p:p+n]
            p += n
            inputs, states = cell.unroll(length, inputs=inputs, begin_state=states, layout=layout,
                                         merge_outputs=None if i < num_cells-1 else merge_outputs)
            next_states.extend(states)

        return inputs, next_states

    def __getitem__(self, i):
        return self._children[i]

    def __len__(self):
        return len(self._children)

    def hybrid_forward(self, *args, **kwargs):
        raise NotImplementedError


class DropoutCell(HybridRecurrentCell):
    """Applies dropout on input.

    Parameters
    ----------
    rate : float
        Percentage of elements to drop out, which
        is 1 - percentage to retain.
    """
    def __init__(self, rate, prefix=None, params=None):
        super(DropoutCell, self).__init__(prefix, params)
        assert isinstance(rate, numeric_types), "rate must be a number"
        self.rate = rate

    def __repr__(self):
        s = '{name}(rate = {rate})'
        return s.format(name=self.__class__.__name__,
                        **self.__dict__)

    def state_info(self, batch_size=0):
        return []

    def _alias(self):
        return 'dropout'

    def hybrid_forward(self, F, inputs, states):
        if self.rate > 0:
            inputs = F.Dropout(data=inputs, p=self.rate, name='t%d_fwd'%self._counter)
        return inputs, states

    def unroll(self, length, inputs, begin_state=None, layout='NTC', merge_outputs=None):
        self.reset()

        inputs, _, F, _ = _format_sequence(length, inputs, layout, merge_outputs)
        if isinstance(inputs, tensor_types):
            return self.hybrid_forward(F, inputs, begin_state if begin_state else [])
        else:
            return super(DropoutCell, self).unroll(
                length, inputs, begin_state=begin_state, layout=layout,
                merge_outputs=merge_outputs)


class ModifierCell(HybridRecurrentCell):
    """Base class for modifier cells. A modifier
    cell takes a base cell, apply modifications
    on it (e.g. Zoneout), and returns a new cell.

    After applying modifiers the base cell should
    no longer be called directly. The modifier cell
    should be used instead.
    """
    def __init__(self, base_cell):
        assert not base_cell._modified, \
            "Cell %s is already modified. One cell cannot be modified twice"%base_cell.name
        base_cell._modified = True
        super(ModifierCell, self).__init__(prefix=base_cell.prefix+self._alias(),
                                           params=None)
        self.base_cell = base_cell

    @property
    def params(self):
        return self.base_cell.params

    def state_info(self, batch_size=0):
        return self.base_cell.state_info(batch_size)

    def begin_state(self, func=symbol.zeros, **kwargs):
        assert not self._modified, \
            "After applying modifier cells (e.g. DropoutCell) the base " \
            "cell cannot be called directly. Call the modifier cell instead."
        self.base_cell._modified = False
        begin = self.base_cell.begin_state(func=func, **kwargs)
        self.base_cell._modified = True
        return begin

    def hybrid_forward(self, F, inputs, states):
        raise NotImplementedError

    def __repr__(self):
        s = '{name}({base_cell})'
        return s.format(name=self.__class__.__name__,
                        **self.__dict__)


class ZoneoutCell(ModifierCell):
    """Applies Zoneout on base cell."""
    def __init__(self, base_cell, zoneout_outputs=0., zoneout_states=0.):
        assert not isinstance(base_cell, BidirectionalCell), \
            "BidirectionalCell doesn't support zoneout since it doesn't support step. " \
            "Please add ZoneoutCell to the cells underneath instead."
        assert not isinstance(base_cell, SequentialRNNCell) or not base_cell._bidirectional, \
            "Bidirectional SequentialRNNCell doesn't support zoneout. " \
            "Please add ZoneoutCell to the cells underneath instead."
        super(ZoneoutCell, self).__init__(base_cell)
        self.zoneout_outputs = zoneout_outputs
        self.zoneout_states = zoneout_states
        self.prev_output = None

    def __repr__(self):
        s = '{name}(p_out={zoneout_outputs}, p_state={zoneout_states}, {base_cell})'
        return s.format(name=self.__class__.__name__,
                        **self.__dict__)

    def _alias(self):
        return 'zoneout'

    def reset(self):
        super(ZoneoutCell, self).reset()
        self.prev_output = None

    def hybrid_forward(self, F, inputs, states):
        cell, p_outputs, p_states = self.base_cell, self.zoneout_outputs, self.zoneout_states
        next_output, next_states = cell(inputs, states)
        mask = (lambda p, like: F.Dropout(F.ones_like(like), p=p))

        prev_output = self.prev_output
        if prev_output is None:
            prev_output = F.zeros_like(next_output)

        output = (F.where(mask(p_outputs, next_output), next_output, prev_output)
                  if p_outputs != 0. else next_output)
        states = ([F.where(mask(p_states, new_s), new_s, old_s) for new_s, old_s in
                   zip(next_states, states)] if p_states != 0. else next_states)

        self.prev_output = output

        return output, states


class ResidualCell(ModifierCell):
    """
    Adds residual connection as described in Wu et al, 2016
    (https://arxiv.org/abs/1609.08144).
    Output of the cell is output of the base cell plus input.
    """

    def __init__(self, base_cell):
        super(ResidualCell, self).__init__(base_cell)

    def hybrid_forward(self, F, inputs, states):
        output, states = self.base_cell(inputs, states)
        output = F.elemwise_add(output, inputs, name='t%d_fwd'%self._counter)
        return output, states

    def unroll(self, length, inputs, begin_state=None, layout='NTC', merge_outputs=None):
        self.reset()

        self.base_cell._modified = False
        outputs, states = self.base_cell.unroll(length, inputs=inputs, begin_state=begin_state,
                                                layout=layout, merge_outputs=merge_outputs)
        self.base_cell._modified = True

        merge_outputs = isinstance(outputs, tensor_types) if merge_outputs is None else \
                        merge_outputs
        inputs, _, F, _ = _format_sequence(length, inputs, layout, merge_outputs)
        if merge_outputs:
            outputs = F.elemwise_add(outputs, inputs)
        else:
            outputs = [F.elemwise_add(i, j) for i, j in zip(outputs, inputs)]

        return outputs, states


class BidirectionalCell(HybridRecurrentCell):
    """Bidirectional RNN cell.

    Parameters
    ----------
    l_cell : RecurrentCell
        Cell for forward unrolling
    r_cell : RecurrentCell
        Cell for backward unrolling
    """
    def __init__(self, l_cell, r_cell, output_prefix='bi_'):
        super(BidirectionalCell, self).__init__(prefix='', params=None)
        self.register_child(l_cell)
        self.register_child(r_cell)
        self._output_prefix = output_prefix

    def __call__(self, inputs, states):
        raise NotImplementedError("Bidirectional cannot be stepped. Please use unroll")

    def __repr__(self):
        s = '{name}(forward={l_cell}, backward={r_cell})'
        return s.format(name=self.__class__.__name__,
                        l_cell=self._children[0],
                        r_cell=self._children[1])

    def state_info(self, batch_size=0):
        return _cells_state_info(self._children, batch_size)

    def begin_state(self, **kwargs):
        assert not self._modified, \
            "After applying modifier cells (e.g. DropoutCell) the base " \
            "cell cannot be called directly. Call the modifier cell instead."
        return _cells_begin_state(self._children, **kwargs)

    def unroll(self, length, inputs, begin_state=None, layout='NTC', merge_outputs=None):
        self.reset()

        inputs, axis, F, batch_size = _format_sequence(length, inputs, layout, False)
        begin_state = _get_begin_state(self, F, begin_state, inputs, batch_size)

        states = begin_state
        l_cell, r_cell = self._children
        l_outputs, l_states = l_cell.unroll(length, inputs=inputs,
                                            begin_state=states[:len(l_cell.state_info(batch_size))],
                                            layout=layout, merge_outputs=merge_outputs)
        r_outputs, r_states = r_cell.unroll(length,
                                            inputs=list(reversed(inputs)),
                                            begin_state=states[len(l_cell.state_info(batch_size)):],
                                            layout=layout, merge_outputs=merge_outputs)

        if merge_outputs is None:
            merge_outputs = (isinstance(l_outputs, tensor_types)
                             and isinstance(r_outputs, tensor_types))
            l_outputs, _, _, _ = _format_sequence(None, l_outputs, layout, merge_outputs)
            r_outputs, _, _, _ = _format_sequence(None, r_outputs, layout, merge_outputs)

        if merge_outputs:
            r_outputs = F.reverse(r_outputs, axis=axis)
            outputs = F.concat(l_outputs, r_outputs, dim=2, name='%sout'%self._output_prefix)
        else:
            outputs = [F.concat(l_o, r_o, dim=1, name='%st%d'%(self._output_prefix, i))
                       for i, (l_o, r_o) in enumerate(zip(l_outputs, reversed(r_outputs)))]

        states = l_states + r_states
        return outputs, states
