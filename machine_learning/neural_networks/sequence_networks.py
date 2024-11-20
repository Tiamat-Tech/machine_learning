# standard libraries
import pdb
import os
import math
import copy
from functools import reduce
from termcolor import cprint
from IPython.display import clear_output
import pickle

# third-party libraries
import numpy as np
from scipy.special import logsumexp
import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow.python import pywrap_tensorflow
try:
    from tensor2tensor.layers import common_layers
    from tensor2tensor.utils import beam_search as beam_search
except ModuleNotFoundError:
    print('WARNING: tensor2tensor missing; skipping')
try:
    import tfmpl
except ModuleNotFoundError:
    print('Package conflict (probably because you are using TF2.x)', end='')
    print('...not loading tfmpl...')


# local
from utils_jgm import toolbox
from utils_jgm.machine_compatibility_utils import MachineCompatibilityUtils
from machine_learning.neural_networks import basic_components as nn
from machine_learning.neural_networks import tf_helpers as tfh

MCUs = MachineCompatibilityUtils()

'''
Neural networks for sequence-to-label and sequence-to-sequence problems.
 The bulk of this module consists of the class SequenceNetwork, whose main
 (external-facing) methods are .fit and .assess.
 Etc....

 :Author: J.G. Makin (except where otherwise noted)

Created: July 2017
  by JGM
'''


@tfmpl.figure_tensor
def dual_violin_plot(
    data, labels, label_list, x_axis_label=None, y_axis_label=None,
    ymin=None, ymax=None, figsize=(12, 12)
):
    fig = tfmpl.create_figure(figsize=figsize)
    ax = fig.add_subplot(111)
    # ax.axis('off')
    # ax.scatter(x, y)
    ax.violinplot(
        dataset=[
            data[labels == label] for label in label_list
            if data[labels == label].shape[0]
        ],
        positions=[
            label for label in label_list if data[labels == label].shape[0]
        ],
        showmeans=False, showmedians=True
    )
    ax.set_xlabel(x_axis_label)
    ax.set_ylabel(y_axis_label)
    ax.set_xticks(label_list)
    ax.set_xticklabels(label_list)
    ax.set_ylim((ymin, ymax))
    fig.tight_layout()

    return fig


def single_word_predictions(word, targets_list, targets_given_predictions):

    prediction_counts_vector = targets_given_predictions[
        targets_list.index(word)]
    predicted_words = list(np.array(targets_list)[
        prediction_counts_vector > 0])
    prediction_counts = list(prediction_counts_vector[
        prediction_counts_vector > 0])

    predicted_words_and_counts = [(word, count) for word, count in
                                  zip(predicted_words, prediction_counts)]

    return predicted_words_and_counts


def _transpose_annotator(TRANSPOSED):
    def wrapper(affine_fxn):
        affine_fxn.TRANSPOSED = TRANSPOSED
        return affine_fxn
    return wrapper


# a class for the encoder-decoder network
class SequenceNetwork:
    @toolbox.auto_attribute(CHECK_MANIFEST=True)
    def __init__(
        self,
        manifest,
        #####
        # kwargs set in the manifest
        temperature=None,
        N_epochs=None,
        layer_sizes=None,
        FF_dropout=None,
        RNN_dropout=None,
        EMA_decay=None,
        beam_width=None,
        TEMPORALLY_CONVOLVE=None,
        assessment_epoch_interval=None,
        tf_summaries_dir=None,
        #####
        N_cases=256,
        stiffness=0,
        beam_alpha=0.6,
        max_hyp_length=20,
        EOS_token='<EOS>',
        pad_token='<pad>',
        OOV_token='<OOV>',
        assessment_partitions=None,
        num_guessable_classes=None,
        num_training_shards_to_discard=0,
        checkpoint_path='~/tmp/checkpoint_data/model.ckpt',
        TARGETS_ARE_SEQUENCES=True,
        ASSESS_ALL_DECIMATIONS=True,
        ENCODER_RNN_IS_BIDIRECTIONAL=True,
        MAX_POOL=False,
        BIAS_DECODER_OUTPUTS=False,  # to match seq2seq
        k_for_top_k_accuracy=5,
        assessment_op_set={
            'decoder_word_error_rate',
            'decoder_accuracy',
            'decoder_confusions',
            'decoder_top_k_inds',
            'decoder_sequence_log_probs',
            'decoder_targets',
            'decoder_beam_targets',
            "decoder_outputs",
        },
        summary_op_set={
            'decoder_word_error_rate',
            'decoder_accuracy',
            'decoder_confusions_image',
            'decoder_top_k_accuracy',
            # 'decoder_xpct_normalized_accuracy',
            # 'decoder_vrnc_normalized_accuracy',
            # 'decoder_entropy',
            # 'decoder_calibration',
            # 'decoder_calibration_image',
        },
        PROBABILISTIC_CONFUSIONS=False,
        inputs_to_occlude=None,
        training_GPUs=None,  # let your code decide where to put things
        VERBOSE=True,
        # private; don't assign these to self:
        # ...
    ):

        ######
        # Is this still necessary for the Windows version of tf??
        # self.allow_gpu_growth = True
        ######

        if training_GPUs is None:
            self.assessment_GPU = 1 if MCUs.num_GPUs > 1 else 0
        else:
            # This is dubious: it should probably just pick whatever GPU is
            #  *not* being used for training--if there is one
            self.assessment_GPU = 1

        self.num_CPUs = MCUs.num_CPUs
        self.num_seq2seq_shards = None

        # announce
        self.vprint('Creating a sequence network that will train on ', end='')
        self.vprint('%2.0f%% of the training data' %
                    (100/(self.num_training_shards_to_discard+1)))

        # if you TEMPORALLY_CONVOLVE make sure you don't ASSESS_ALL_DECIMATIONS
        if self.TEMPORALLY_CONVOLVE:
            self.vprint('Temporal convolution; enforcing ASSESS_ALL_DECIMATIONS = False...')
            self.ASSESS_ALL_DECIMATIONS = False

        # what partitions of the data should you assess the network on?
        if self.assessment_partitions is None:
            self.assessment_partitions = ['training', 'validation']

        # check that the encoder and decoder sizes all work together
        for e_size, d_size in zip(
            self.layer_sizes['encoder_rnn'], self.layer_sizes['decoder_rnn']
        ):
            assert d_size == (1+self.ENCODER_RNN_IS_BIDIRECTIONAL)*e_size, \
                   "encoder/decoder layer-size mismatch!"

        # adjust the summary_op_set so that tensorboard shows top_k nicely
        for op_name in self.summary_op_set:
            if op_name.endswith('top_k_accuracy'):
                # if 'decoder_top_k_accuracy' in self.summary_op_set:
                self.summary_op_set.remove(op_name)
                self.summary_op_set.add(op_name.replace(
                    'top_k_accuracy', 'top_%i_accuracy' % k_for_top_k_accuracy
                ))

    def vprint(self, *args, **kwargs):
        if self.VERBOSE:
            print(*args, **kwargs)

    def _initialize_assessment_struct(
            self, initialize_data, data_type, num_epochs):

        # set up a structure for items to be assessed and returned
        class AssessmentTuple(toolbox.MutableNamedTuple):
            __slots__ = ([
                'writer', 'initializer', 'decoder_accuracies',
                'decoder_word_error_rates'
            ] + list(self.assessment_op_set))

        nums_assessments = math.ceil(num_epochs/self.assessment_epoch_interval)+1
        return AssessmentTuple(
            initializer=initialize_data,
            writer=tf.compat.v1.summary.FileWriter(os.path.join(
                self.tf_summaries_dir, data_type)),
            decoder_accuracies=np.zeros((nums_assessments)),
            decoder_word_error_rates=np.zeros((nums_assessments)),
            **dict.fromkeys(self.assessment_op_set)
        )

    def fit(
        self, subnets_params, train_vars_scope=None, reuse_vars_scope=None,
        **graph_kwargs
    ):
        '''
        Fit the parameters of a neural network mapping variable-length
        sequences to labels or to (variable-length) output sequences.
        '''

        # dump to disk a copy of each categorical var's feature_list
        for data_manifest in subnets_params[-1].data_manifests.values():
            if data_manifest.distribution == 'categorical':
                file_name = '_'.join([data_manifest.sequence_type, 'vocab_file.pkl'])
                with open(os.path.join(
                    os.path.dirname(self.checkpoint_path), file_name
                ), 'wb') as fp:
                    feature_list = [
                        t.encode('utf-8')
                        for t in data_manifest.get_feature_list()
                    ]
                    pickle.dump(feature_list, fp)

        # init
        with tf.device('/cpu:0'):
            optimizer = tf.compat.v1.train.AdamOptimizer(
                self.compute_learning_rate(subnets_params, 813))
            # I think the issue here is AdaM: it prefers to start with learning
            #  rates near 3e-4, independent of the total number of training
            #  data. So just hard-code Ntotal = 813 to yield 3e-4 with temp=0.4

            '''
            optimizer = tf.contrib.opt.AdamWOptimizer(
                weight_decay=0.01,
                learning_rate=10*self.compute_learning_rate(
                    subnets_params, self.N_cases, 813)/2
            )
            '''

            # But remember to adjust for the *effective* batch size,
            #   num_cases*len(get_available_gpus())!!

        # only the *last* subnet is used for evaluation
        assessment_subnet_params = subnets_params[-1]
        decoder_targets_list = assessment_subnet_params.data_manifests[
            'decoder_targets'].get_feature_list()

        def training_data_fxn(num_GPUs):
            return self._batch_and_split_data(subnets_params, num_GPUs)

        def assessment_data_fxn(num_epochs):
            return self._generate_oneshot_datasets(
                assessment_subnet_params, num_epochs
            )

        def training_net_builder(GPU_op_dict, CPU_op_dict, tower_name):
            return self._build_training_net(
                GPU_op_dict, CPU_op_dict, subnets_params,
                (decoder_targets_list.index(self.EOS_token)
                 if self.EOS_token in decoder_targets_list else None),
                train_vars_scope, tower_name)

        @tfmpl.figure_tensor
        def plotting_fxn(confusions, axis_labels):
            fig = toolbox.draw_confusion_matrix(
                confusions, axis_labels, (12, 12)
            )
            return fig

        def assessment_net_builder(GPU_op_dict, CPU_op_dict):
            return self._build_assessment_net(
                GPU_op_dict, CPU_op_dict, assessment_subnet_params,
                self._standard_indexer, plotting_fxn)

        def assessor(
            sess, assessment_struct, epoch, assessment_step, data_partition
        ):
            return self._assess(
                sess, assessment_struct, epoch, assessment_step,
                decoder_targets_list, data_partition)

        # use the general graph build to assemble these pieces
        graph_builder = tfh.GraphBuilder(
            training_data_fxn, assessment_data_fxn, training_net_builder,
            assessment_net_builder, optimizer, assessor, self.checkpoint_path,
            self.N_epochs,
            EMA_decay=self.EMA_decay, reuse_vars_scope=reuse_vars_scope,
            training_GPUs=self.training_GPUs, assessment_GPU=self.assessment_GPU,
            **graph_kwargs
        )
        return graph_builder.train_and_assess(self.assessment_epoch_interval)

    def restore_and_get_saliencies(
        self, subnets_params, restore_epoch, assessment_type='norms',
        data_partition='validation', **graph_kwargs
    ):

        # init
        tf.compat.v1.reset_default_graph()
        FF_dropout = self.FF_dropout
        self.FF_dropout = 0.0
        RNN_dropout = self.RNN_dropout
        self.RNN_dropout = 0.0
        decoder_targets_list = subnets_params[-1].data_manifests[
            'decoder_targets'].get_feature_list()

        class FakeOptimizer:
            def __init__(self):
                pass

            def compute_gradients(self, total_loss, get_inputs):
                # In fact, you have to return gradients and variables
                return [(g, None) for g in tf.gradients(ys=total_loss, xs=get_inputs)]

        optimizer = FakeOptimizer()

        def training_data_fxn(num_GPUs):
            return self._batch_and_split_data(
                subnets_params, num_GPUs, data_partition
            )

        def training_net_builder(GPU_op_dict, CPU_op_dict, tower_name):

            tf.transpose(
                a=GPU_op_dict['decoder_targets'], perm=[0, 2, 1],
                name='assess_decoder_targets'
            )

            total_loss, train_vars = self._build_training_net(
                GPU_op_dict, CPU_op_dict, subnets_params,
                (decoder_targets_list.index(self.EOS_token)
                 if self.EOS_token in decoder_targets_list else None),
                None, tower_name)
            return total_loss, GPU_op_dict['encoder_inputs']

        def get_saliency_sequences(sess, initializer, get_input_saliencies):
            # get the full sequences of dL/dinput--FOR ONE BATCH
            sess.run(initializer)
            return sess.run((
                get_input_saliencies,
                sess.graph.get_operation_by_name(
                    'tower_0/assess_decoder_targets').outputs[0]
                ))

        def get_saliency_norms(sess, initializer, get_input_saliencies):
            # desequence, take norm--across time and sequences
            index_sequences, _ = nn.sequences_tools(get_input_saliencies)  #[:, :50]
            desequence_saliencies = tf.gather_nd(
                get_input_saliencies, index_sequences)
            get_squared_magnitudes = tf.reduce_sum(
                tf.square(desequence_saliencies), axis=0)

            # accumulate gradient norm across batches
            sess.run(initializer)
            accumulated_saliences = np.zeros(
                (subnets_params[-1].data_manifests['encoder_inputs'].num_features))
            while True:
                try:
                    accumulated_saliences += sess.run(get_squared_magnitudes)
                except tf.errors.OutOfRangeError:
                    break
            return np.sqrt(accumulated_saliences)

        def get_per_class_saliency_norms(sess, initializer, get_input_saliencies):
            # take norm across time
            get_per_example_norms = tf.sqrt(tf.reduce_sum(
                tf.square(get_input_saliencies), axis=1)
            )

            # ...
            all_per_example_norms = np.zeros(
                [0, subnets_params[-1].data_manifests['encoder_inputs'].num_features])
            ######
            # This won't work for word sequences (right?)
            all_decoder_targets = np.zeros([0, 1, 1])
            ######
            sess.run(initializer)
            while True:
                try:
                    per_example_norms, decoder_targets = sess.run((
                        get_per_example_norms,
                        sess.graph.get_operation_by_name(
                            'tower_0/assess_decoder_targets').outputs[0]
                    ))
                    all_per_example_norms = np.concatenate(
                        (all_per_example_norms, per_example_norms)
                    )
                    all_decoder_targets = np.concatenate(
                        (all_decoder_targets, decoder_targets)
                    )
                except tf.errors.OutOfRangeError:
                    break
            return all_per_example_norms, decoder_targets

        ######
        # For now, at least, abuse the assessor
        assessor_dict = {
            'sequences': get_saliency_sequences,
            'norms': get_saliency_norms,
            'per_class_norms': get_per_class_saliency_norms,
        }
        ######

        # use the general graph build to assemble these pieces
        graph_builder = tfh.GraphBuilder(
            training_data_fxn, None, training_net_builder, None, optimizer,
            assessor_dict[assessment_type], self.checkpoint_path,
            restore_epoch, restore_epoch-1, EMA_decay=self.EMA_decay,
            training_GPUs=self.training_GPUs, **graph_kwargs
        )
        saliencies = graph_builder.get_saliencies()

        # restore
        self.FF_dropout = FF_dropout
        self.RNN_dropout = RNN_dropout

        return saliencies

    def _build_training_net(
        self, sequenced_op_dict, CPU_op_dict, subnets_params, eos_id,
        train_vars_scope, tower_name
    ):
        '''
        The neural network to be trained
        '''

        # augment the encoder data?
        sequenced_op_dict = data_augmentor(sequenced_op_dict, 'encoder_')

        # build the training NN
        with tf.compat.v1.variable_scope('seq2seq', reuse=tf.compat.v1.AUTO_REUSE):

            # tensorflow requires that *something* be returned
            final_RNN_states = tf.case([(
                tf.equal(CPU_op_dict['subnet_id'], subnet_params.subnet_id),
                lambda params=subnet_params: self._build_training_net_core(
                    sequenced_op_dict, params, tower_name, eos_id
                )
            ) for subnet_params in subnets_params], exclusive=True)

            # only train the part of the graph given by train_vars_scope
            total_loss = tf.add_n(
                [loss for loss in tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.LOSSES)
                 if loss.name.startswith(tower_name)]
            )
            train_vars = tf.compat.v1.get_collection(
                tf.compat.v1.GraphKeys.TRAINABLE_VARIABLES, scope=train_vars_scope)

        return total_loss, train_vars

    def _build_training_net_core(
        self, sequenced_op_dict, subnet_params, tower_name, eos_id
    ):

        # ENCODER
        (sequenced_op_dict, final_encoder_states, stride, draw_initial_ind,
         ) = self._encode_sequences(
            sequenced_op_dict, subnet_params, self.FF_dropout,
            self.RNN_dropout, tower_name=tower_name
        )
        sequenced_op_dict = self._prepare_encoder_targets(
            sequenced_op_dict, draw_initial_ind, stride
        )

        # DECODER
        decode_training_data = (
            self._decode_training_sequences if self.TARGETS_ARE_SEQUENCES
            else self._decode_training_tokens
        )
        sequenced_op_dict = decode_training_data(
            sequenced_op_dict, final_encoder_states, subnet_params,
            self.FF_dropout, eos_id
        )

        ######
        # NB: Putting this in the if statement means a (nominally) different
        #  loss will be used for different subjects during *parallel* transfer
        #  learning.
        ######
        # apply to every *_target in the data_manifests
        for t_key, data_manifest in subnet_params.data_manifests.items():
            if '_targets' in t_key:
                compute_cross_entropy = nn.cross_entropy(
                    t_key, data_manifest, sequenced_op_dict
                )
                tf.compat.v1.add_to_collection(
                    tf.compat.v1.GraphKeys.LOSSES,
                    compute_cross_entropy*data_manifest.penalty_scale
                )

        return final_encoder_states

        ##########
    def save_prediction_graph(
        self, subnets_params, restore_epoch, save_dir, num_sequences=1,
        inputs_dtype=tf.float32
    ):
        '''
        Save the graph so that it can later be loaded and used for inference.

        :param subnets_params:
        :param restore_epoch:
        :param N
        :param inputs_dtype:
        :return: predict, a function that takes input sequences (numpy arrays)
            as input, and returns the target predictions as unnormalized log
            probabilities
        '''

        # init
        this_checkpoint = self.checkpoint_path + '-%i' % restore_epoch
        inputs_shape = [
            num_sequences,
            None,
            subnets_params[0].data_manifests['encoder_inputs'].num_features
        ]
        decoder_targets_list = subnets_params[0].data_manifests[
            'decoder_targets'].get_feature_list()

        # the inference graph
        prediction_graph = tf.Graph()
        with prediction_graph.as_default():

            place_encoder_inputs = tf.compat.v1.placeholder(
                dtype=inputs_dtype, shape=inputs_shape, name='encoder_inputs')

            # NB that this is only used when TARGETS_ARE_SEQUENCES
            def indexing_fxn(targets):
                row_inds, col_inds = tf.meshgrid(
                    tf.range(num_sequences),
                    tf.range(self.max_hyp_length), indexing='ij')
                index_sequences_elements = tf.stack((
                    tf.reshape(row_inds, [-1]), tf.reshape(col_inds, [-1])), 1)

                return index_sequences_elements, self.max_hyp_length

            # build assessment graph
            get_sequenced_outputs, get_sequenced_natural_params = \
                self._build_assessment_net(
                    {
                        'encoder_inputs': place_encoder_inputs,
                        'decoder_targets': tf.cast(np.transpose(
                            np.array([[[]]]), axes=[0, 2, 1]), tf.int32),
                        # Since this graph will be used only for inference
                        #  there's no point in saving the parts associated
                        #  with encoder_targets, which are *auxiliary*.
                        'encoder_1_targets': tf.cast([[[]]], tf.float32),
                    },
                    {
                        'subnet_id': tf.constant(subnets_params[0].subnet_id)
                    },
                    subnets_params[0], indexing_fxn, None
                )
            get_decoder_probs = tf.nn.softmax(tf.reshape(
                get_sequenced_natural_params, [-1, len(decoder_targets_list)]
            ))

            # if no decoder_targets_list was passed...
            get_sequenced_decoder_outputs = tf.identity(
                get_sequenced_outputs, name='decoder_outputs')
            get_decoder_probs = tf.identity(
                get_decoder_probs, name='decoder_probs')

            # set up the session, restoring the model at this_checkpoint
            EMA = (tf.train.ExponentialMovingAverage(decay=self.EMA_decay)
                   if self.EMA_decay else None)
            sess, saver = tfh.get_session_and_saver(EMA=EMA, allow_growth=True)
            saver.restore(sess, this_checkpoint)

            # save this map from inputs to two outputs
            tf.compat.v1.saved_model.simple_save(
                sess, save_dir,
                inputs={'encoder_inputs': place_encoder_inputs},
                outputs={
                    "decoder_probs": get_decoder_probs,
                    "decoder_outputs": get_sequenced_decoder_outputs
                }
            )

    def _build_assessment_net(
        self, sequenced_op_dict, CPU_op_dict, params, indexing_fxn, plotting_fxn
    ):

        # identify for tensorboard
        tf.identity(CPU_op_dict['subnet_id'], name='identify_subnet_id')

        # reverse and desequence the encoder targets
        stride = params.decimation_factor
        if self.ASSESS_ALL_DECIMATIONS:
            initial_initial_ind = 0
            num_loops = stride
            stride = 1
        else:
            # use the midpoint
            initial_initial_ind = 0  # stride//2
            num_loops = 1
        sequenced_op_dict = self._prepare_encoder_targets(
            sequenced_op_dict, initial_initial_ind, stride
        )

        if self.inputs_to_occlude:
            sequenced_op_dict['encoder_inputs'] = nn.occlude_sequence_features(
                sequenced_op_dict['encoder_inputs'], self.inputs_to_occlude)

        # create the sequence-classification neural network
        sequenced_op_dict = self._decode_assessments(
            sequenced_op_dict, params, initial_initial_ind, num_loops,
            indexing_fxn
        )

        # don't bother to write unless there is something to plot (??)
        if plotting_fxn is not None:
            self._write_assessments(sequenced_op_dict, params, plotting_fxn)

        return (
            sequenced_op_dict['decoder_outputs'],
            sequenced_op_dict['decoder_natural_params']
        )

    def _decode_assessments(
        self, sequenced_op_dict, params, initial_initial_ind, num_loops, indexer
    ):

        # init
        if self.TARGETS_ARE_SEQUENCES:
            update_decoder_assessments = \
                self._update_sequence_assessments
            decode_assessments_core = self._decode_assessment_sequences
        else:
            update_decoder_assessments = \
                self._update_token_assessments
            decode_assessments_core = self._decode_assessment_tokens

        # index the target sequences
        index_decoder_targets, _ = indexer(sequenced_op_dict['decoder_targets'])

        def loop_body(
            initial_ind_op,
            sequenced_natural_params_dict,
            concatenate_sequenced_decoder_outputs,
            concatenate_decoder_sequence_log_probs,
        ):
            nonlocal sequenced_op_dict

            with tf.compat.v1.variable_scope(
                'seq2seq', reuse=tf.compat.v1.AUTO_REUSE
            ):

                # encode decimated sequence starting at initial_ind
                sequenced_op_dict, final_encoder_states, _, _ = self._encode_sequences(
                    sequenced_op_dict, params, 0.0, 0.0,
                    set_initial_ind=initial_ind_op
                )

                # *fill in* the *encoder* natural params for each initial_ind
                sequenced_natural_params_dict = self._fill_in_decimated_sequences(
                    initial_ind_op, num_loops, sequenced_op_dict,
                    sequenced_natural_params_dict
                )

                # *accumulate* *decoder* natural params across all initial_inds
                (sequenced_natural_params_dict,
                 concatenate_sequenced_decoder_outputs,
                 concatenate_decoder_sequence_log_probs
                 ) = decode_assessments_core(
                    params, final_encoder_states, sequenced_op_dict,
                    sequenced_natural_params_dict,
                    concatenate_sequenced_decoder_outputs,
                    concatenate_decoder_sequence_log_probs
                )

                # help out tf's shape-inference engine--doesn't seem like it
                #  ought to be necessary but it is
                for np_key, np_op in sequenced_natural_params_dict.items():
                    t_key = np_key.replace('natural_params', 'targets')
                    np_op.set_shape(
                        [None, None, params.data_manifests[t_key].num_features]
                    )

                return (
                    initial_ind_op+1,
                    sequenced_natural_params_dict,
                    concatenate_sequenced_decoder_outputs,
                    concatenate_decoder_sequence_log_probs,
                )

        # initial values of loop vars
        count_num_cases = tf.shape(sequenced_op_dict['decoder_targets'])[0]
        initial_values = [
            tf.constant(initial_initial_ind),
            {
                nn.swap(key, 'natural_params'): tf.fill([
                    tf.shape(op)[0], tf.shape(op)[1],
                    params.data_manifests[key].num_features
                ], 0.0)
                for key, op in sequenced_op_dict.items() if '_targets' in key
            },
            tf.fill((count_num_cases, 0, self.max_hyp_length), 0),
            tf.fill((count_num_cases, 0), 0.0),
        ]
        ######
        # count_num_cases is not altogether invariant, but it is invariant
        #  across the while_loop.  It feels like you should therefore be
        #  able to communicate this.
        shape_invariants = [
            tf.TensorShape([]),
            {
                nn.swap(key, 'natural_params'): tf.TensorShape([
                    None, None, params.data_manifests[key].num_features
                ])
                for key in sequenced_op_dict if '_targets' in key
            },
            tf.TensorShape([None, None, None]),
            tf.TensorShape([None, None]),
        ]
        ######

        # run the loop
        (_, sequenced_natural_params_dict,
         sequenced_op_dict['decoder_outputs'],
         sequenced_op_dict['decoder_sequence_log_probs'],
         ) = tf.while_loop(
            cond=lambda initial_ind, aa, bb, cc:
                initial_ind < (initial_initial_ind+num_loops),
            body=loop_body, loop_vars=initial_values,
            shape_invariants=shape_invariants, back_prop=False
        )
        sequenced_op_dict = {
            **sequenced_op_dict, **sequenced_natural_params_dict
        }

        # and now update the sequenced_op_dict
        sequenced_op_dict = update_decoder_assessments(
            'decoder_targets', sequenced_op_dict, params,
            index_decoder_targets, num_loops
        )

        # If any encoder targets are categorically distributed, collect the
        #   tensors required to compute a word error rate.
        for key, data_manifest in params.data_manifests.items():
            if key.endswith('targets') and key.startswith('encoder'):
                if data_manifest.distribution == 'categorical':
                    # no beam search for the encoder (but see notes)
                    sequenced_op_dict[nn.swap(key, 'beam_targets')] = \
                        tf.transpose(sequenced_op_dict[key], perm=[0, 2, 1])
                    sequenced_op_dict[nn.swap(key, 'outputs')] = tf.expand_dims(
                        tf.argmax(
                            sequenced_op_dict[nn.swap(key, 'natural_params')],
                            axis=2, output_type=tf.int32
                        ), 1
                    )
                    sequenced_op_dict[nn.swap(key, 'sequence_log_probs')] = \
                        tf.fill([tf.shape(sequenced_op_dict[key])[0], 1], 0.0)

        return sequenced_op_dict

    def _decode_assessment_tokens(
        self, params, final_encoder_states, sequenced_op_dict,
        sequenced_natural_params_dict, concatenate_sequenced_decoder_outputs,
        concatenate_decoder_sequence_log_probs
    ):
        # *sum* the *decoder* natural params across all initial_inds
        sequenced_op_dict = self._decode_training_tokens(
            sequenced_op_dict, final_encoder_states, params, 0.0, None
        )
        sequenced_natural_params_dict['decoder_natural_params'] += \
            sequenced_op_dict['decoder_natural_params']

        return (
            sequenced_natural_params_dict,
            concatenate_sequenced_decoder_outputs,
            concatenate_decoder_sequence_log_probs
        )

    def _decode_assessment_sequences(
        self, params, final_encoder_states, sequenced_op_dict,
        sequenced_natural_params_dict, concatenate_sequenced_decoder_outputs,
        concatenate_decoder_sequence_log_probs
    ):
        (get_sequenced_decoder_outputs, get_decoder_sequence_log_probs
         ) = self._decode_assessment_sequences_core(
            final_encoder_states, sequenced_op_dict['decoder_targets'], params,
            params.data_manifests['decoder_targets'].get_feature_list(),
        )

        # as though the beam were (beam_width*temporal stride) wide
        targ_length = tf.shape(get_sequenced_decoder_outputs)[2]
        paddings = [[0, 0], [0, 0], [0, self.max_hyp_length-targ_length]]
        get_sequenced_decoder_outputs = tf.pad(
            tensor=get_sequenced_decoder_outputs, paddings=paddings,
            constant_values=params.data_manifests[
                'decoder_targets'].padding_value
        )
        concatenate_sequenced_decoder_outputs = tf.concat(
            (concatenate_sequenced_decoder_outputs,
             get_sequenced_decoder_outputs), axis=1)
        concatenate_decoder_sequence_log_probs = tf.concat(
            (concatenate_decoder_sequence_log_probs,
             get_decoder_sequence_log_probs), axis=1)

        return (
            sequenced_natural_params_dict,
            concatenate_sequenced_decoder_outputs,
            concatenate_decoder_sequence_log_probs
        )

    def _decode_assessment_sequences_core(
        self, final_encoder_states, get_targets, subnet_params,
        decoder_targets_list,
    ):

        eos_id = decoder_targets_list.index(self.EOS_token)
        Nsequences = common_layers.shape_list(final_encoder_states[-1].h)[0]
        num_decoder_target_features = subnet_params.data_manifests[
            'decoder_targets'].num_features

        if self.num_guessable_classes:
            if get_targets is None:
                print("can't restrict dictionary--targets are unknown")
            else:
                get_guess_indices = self._compute_guessable_class_indices(
                    get_targets, subnet_params)

        def prev_symbols_to_natural_params(decoded_symbols, _, states):
            '''
            Takes the currently decoded symbols and returns the natural
            parameters for the next symbol.  For categorical distributions, the
            natural parameters are (unnormalized) log probabilities.
                Input:
                    decoded_symbols [Nsequences*beam_width, decoded_ids]
                    states          the RNN hidden state
                Output:
                    get_next_token_natural_params
                        [Nsequences, num_decoder_target_features]
            '''
            z, Ninputs = nn.feed_forward_multi_layer(
                decoded_symbols[:, -1, None], num_decoder_target_features,
                self.layer_sizes['decoder_embedding'], 0.0, 'decoder_embedding',
                preactivation_fxns=[self._t2t_embedding_affine_fxn]*len(
                    self.layer_sizes['decoder_embedding'])
            )
            z, decoder_state = nn.LSTM_rnn(
                tf.cast(tf.expand_dims(z, axis=1), tf.float32), None,
                self.layer_sizes['decoder_rnn'], 0.0, 'decoder_rnn',
                initial_state=states["decoder state"])
            get_next_token_natural_params = self._output_net(
                tf.squeeze(z, [1]),
                self.layer_sizes['decoder_rnn'][-1],
                self.layer_sizes['decoder_projection'],
                num_decoder_target_features,
                0.0, final_preactivation=self._t2t_final_affine_fxn)
            if self.num_guessable_classes and (get_targets is not None):
                get_next_token_natural_params = self._reduced_classes_hack(
                    get_next_token_natural_params, get_guess_indices)

            return get_next_token_natural_params, {"decoder state": decoder_state}

        # could replace with tf.contrib.seq2seq.BeamSearchDecoder
        initial_ids = tf.fill([Nsequences], eos_id)
        (get_sequenced_decoder_outputs, get_decoder_sequence_log_probs, _
         ) = beam_search.beam_search(
            prev_symbols_to_natural_params, initial_ids, self.beam_width,
            self.max_hyp_length, num_decoder_target_features, self.beam_alpha,
            states={"decoder state": final_encoder_states}, eos_id=eos_id
        )

        # make sure that the sequences terminate with either <EOS> or <pad>
        get_sequenced_decoder_outputs = self._set_final_nonpads(
            get_sequenced_decoder_outputs,
            eos_id,
            subnet_params.data_manifests['decoder_targets'].padding_value
        )

        # outputs
        return get_sequenced_decoder_outputs, get_decoder_sequence_log_probs

    def _update_token_assessments(
        self, key, sequenced_op_dict, params, index_targets, num_loops
    ):
        sequenced_op_dict[nn.swap(key, 'natural_params')] /= num_loops

        # Only does something interesting for 'trial' data
        (sequenced_op_dict[nn.swap(key, 'beam_targets')],
         sequenced_op_dict[nn.swap(key, 'outputs')],
         sequenced_op_dict[nn.swap(key, 'sequence_log_probs')]
         ) = nn.fake_beam_for_sequence_targets(
            tf.squeeze(sequenced_op_dict[key], [1]),
            tf.squeeze(sequenced_op_dict[nn.swap(key, 'natural_params')], [1]),
            params.data_manifests[key].get_feature_list(),
            self.beam_width, self.pad_token
        )

        return sequenced_op_dict

    def _update_sequence_assessments(
        self, key, sequenced_op_dict, params, index_targets, num_loops
    ):
        # convert: beam, sequence log probs -> token, all-word log probs
        desequenced_natural_params = nn.seq_log_probs_to_word_log_probs(
            sequenced_op_dict[nn.swap(key, 'outputs')],
            sequenced_op_dict[nn.swap(key, 'sequence_log_probs')],
            params.data_manifests[key].num_features,
            index_targets, self.max_hyp_length,
            params.data_manifests[key].padding_value,
        )

        # resequence
        get_targets = sequenced_op_dict[key]
        index_targets, _ = nn.sequences_tools(get_targets)
        resequenced_shape = [
            tf.shape(get_targets)[0], tf.shape(get_targets)[0],
            tf.shape(desequenced_natural_params)[-1]
        ]
        sequenced_op_dict[nn.swap(key, 'natural_params')] = tf.scatter_nd(
            index_targets, desequenced_natural_params, resequenced_shape
        )

        # To facilitate printing:
        #   (N_cases x max_ref_length x 1) -> (N_cases x 1 x max_ref_length)
        sequenced_op_dict[nn.swap(key, 'beam_targets')] = tf.transpose(
            sequenced_op_dict[key], perm=[0, 2, 1],
        )

        return sequenced_op_dict

    def _number_sequence_elements(self, get_sequences):
        '''
        Returns something like:
            [[ 0,  1,  2,  3,  0,  0],
             [ 4,  5,  0,  0,  0,  0],
             [ 6,  7,  8,  9, 10, 11],
             [12, 13, 14,  0,  0,  0]]
        I.e., the sequences' (non-zero) elements, which are intitially probably
          vectors, are replaced with integers that number consecutively the
          elements of *all* num_cases sequences.
        '''
        index_sequences, get_lengths = nn.sequences_tools(get_sequences)
        index_consecutively = tf.scatter_nd(
            index_sequences,
            tf.expand_dims(tf.range(tf.reduce_sum(get_lengths)), axis=1),
            [tf.shape(get_sequences)[0], tf.shape(get_sequences)[1], 1]
        )
        return index_consecutively

    def _fill_in_decimated_sequences(
        self, get_initial_ind, num_loops, sequenced_op_dict,
        sequenced_natural_params_dict,
    ):

        for key, sequenced_op in sequenced_op_dict.items():
            if key.endswith('natural_params') and key.startswith('encoder'):
                if num_loops == 1:
                    sequenced_natural_params_dict[key] = sequenced_op
                else:
                    np = sequenced_natural_params_dict[key]
                    row_inds, col_inds = tf.meshgrid(
                        tf.range(0, tf.shape(np)[0]),
                        tf.range(get_initial_ind, tf.shape(np)[1], num_loops),
                        indexing='ij'
                    )
                    indices = tf.stack((
                        tf.reshape(row_inds, [-1]), tf.reshape(col_inds, [-1])
                    ), 1)
                    #######
                    # supposedly tf.tensor_scatter_add now works here....
                    #######
                    sequenced_natural_params_dict[key] += tf.scatter_nd(
                        indices,
                        tf.reshape(sequenced_op, [-1, tf.shape(sequenced_op)[-1]]),
                        tf.shape(np)
                    )

        return sequenced_natural_params_dict

    def _encode_sequences(
        self, sequenced_op_dict, params, FF_dropout, RNN_dropout,
        set_initial_ind=None, tower_name='blank'
    ):
        # Reverse, embed, RNN-encode.  Also penalize encoder outputs.

        # useful parameters for this network
        net_id = params.subnet_id
        stride = params.decimation_factor
        num_encoder_input_features = params.data_manifests[
            'encoder_inputs'].num_features
        subscope = 'subnet_{}'.format(net_id)

        with tf.compat.v1.variable_scope(subscope, reuse=tf.compat.v1.AUTO_REUSE):
            # reverse (a la Sutskever2014)
            _, get_lengths = nn.sequences_tools(tfh.hide_shape(
                sequenced_op_dict['encoder_inputs']
            ))
            reverse_encoder_inputs = tf.reverse_sequence(
                sequenced_op_dict['encoder_inputs'], get_lengths, seq_axis=1,
                batch_axis=0
            )

            # embed
            if self.TEMPORALLY_CONVOLVE:
                get_RNN_inputs, set_initial_ind = self._convolve_sequences(
                    reverse_encoder_inputs, stride, num_encoder_input_features,
                    self.layer_sizes['encoder_embedding'], FF_dropout,
                    'encoder_embedding', tower_name
                )
                # this is bullet-proof even in the case of USE_BIASES or MAX_POOL
                index_decimated_sequences, get_decimated_lengths = nn.sequences_tools(
                    tfh.hide_shape(
                        reverse_encoder_inputs[:, set_initial_ind::stride, :]))
            else:
                print('Decimating at %ix for subnet %i ' % (stride, net_id))
                if set_initial_ind is None:
                    # probably training
                    set_initial_ind = tf.random.uniform([1], 0, stride, tf.int32)[0]
                decimate_inputs = reverse_encoder_inputs[:, set_initial_ind::stride, :]

                # in case called by tf.case, hide possibly incompatible sizes
                index_decimated_sequences, get_decimated_lengths = nn.sequences_tools(
                    tfh.hide_shape(decimate_inputs))
                get_RNN_inputs = self._sequence_embed(
                    tfh.hide_shape(decimate_inputs),
                    [*common_layers.shape_list(decimate_inputs)[0:2],
                     num_encoder_input_features], index_decimated_sequences,
                    self.layer_sizes['encoder_embedding'], FF_dropout,
                    'encoder_embedding')

        # push through all layers of the RNN--one at a time
        all_outputs = []
        all_final_states = []
        for iLayer, layer_size in enumerate(self.layer_sizes['encoder_rnn']):
            get_RNN_inputs, get_RNN_states = nn.LSTM_rnn(
                get_RNN_inputs, get_decimated_lengths, [layer_size], RNN_dropout,
                'encoder_rnn_%i' % iLayer,
                BIDIRECTIONAL=self.ENCODER_RNN_IS_BIDIRECTIONAL
            )
            all_outputs.append(get_RNN_inputs)
            # get_RNN_states is a length-one tuple
            all_final_states.append(get_RNN_states[0])
        all_final_states = tuple(all_final_states)

        # impose penalties on any of the outputs?
        with tf.compat.v1.variable_scope(subscope, reuse=tf.compat.v1.AUTO_REUSE):
            for iLayer, (get_outputs, layer_size) in enumerate(
                zip(all_outputs, self.layer_sizes['encoder_rnn'])
            ):
                t_key = 'encoder_%i_targets' % iLayer
                if t_key in params.data_manifests:

                    # useful strings
                    np_key = nn.swap(t_key, 'natural_params')
                    subnet_name = nn.swap(t_key, 'projection')

                    # penalize this output
                    desequence_RNN_outputs = tf.gather_nd(
                        get_outputs, index_decimated_sequences)
                    ######
                    # Consider making only the last (linear) layer proprietary:
                    #  The subjects have different voices, and in principle
                    #  could in even have different numbers of cepstral
                    #  coefficients--but the early transformations out of
                    #  abstract RNN state could be conserved.
                    ######
                    desequenced_natural_params = self._output_net(
                        desequence_RNN_outputs,
                        layer_size*(1+self.ENCODER_RNN_IS_BIDIRECTIONAL),
                        self.layer_sizes[subnet_name],
                        params.data_manifests[t_key].num_features,
                        FF_dropout, subnet_name=subnet_name
                    )

                    # re-sequence
                    sequenced_op_dict[np_key] = tf.scatter_nd(
                        index_decimated_sequences, desequenced_natural_params,
                        tf.cast([
                            tf.shape(get_outputs)[0],
                            tf.shape(get_outputs)[1],
                            params.data_manifests[t_key].num_features,
                        ], tf.int32))

        # We will initialize the decoder with the hidden states of the last n
        #  layers of the encoder, where n = number of decoder hidden layers
        final_states = all_final_states[-len(self.layer_sizes['decoder_rnn']):]

        return sequenced_op_dict, final_states, stride, set_initial_ind

    def _convolve_sequences(
        self, sequences, total_stride, num_features, layer_sizes,
        FF_dropout, subnet_name, tower_name
    ):

        # probably brittle...
        layer_strides = toolbox.close_factors(total_stride, len(layer_sizes))
        print('Temporally convolving with strides ' + repr(layer_strides))

        # In 'VALID'-style convolution, the data are not padded to accommodate
        #  the filter, and the final (right-most) elements that don't fit a
        #  filter are simply dropped.  Here we pad by a sufficient amount to
        #  ensure that no data are dropped.  There's no danger in padding too
        #  much because we will subsequently extract out only sequences of the
        #  right length by computing get_decimated_lengths on the *inputs* to
        #  the convolution.
        paddings = [[0, 0], [0, 4*total_stride], [0, 0]]
        sequences = tf.pad(tensor=sequences, paddings=paddings)
        set_initial_ind = 0  # stride//2

        # Construct convolutional layers.  For "VALID" vs. "SAME" padding, see
        #   https://stackoverflow.com/questions/37674306/
        preactivation_fxns = [
            lambda inputs, Nin, Nout, stride=layer_stride, name='conv_%i' % iLayer:
                nn.tf_conv2d_wrapper(
                    inputs, Nin, Nout, name=name, stiffness=self.stiffness,
                    filter_width=stride, USE_BIASES=self.MAX_POOL,
                    strides=[1, 1, 1 if self.MAX_POOL else stride, 1],
                ) for iLayer, layer_stride in enumerate(layer_strides)
        ]
        activation_fxns = [
            (lambda inputs, name, stride=layer_stride: nn.tf_max_pool_wrapper(
                inputs, name=name, ksize=[1, 1, stride, 1], ### ksize=[1,1,8,1],
                strides=[1, 1, stride, 1])) if self.MAX_POOL else
            (lambda inputs, name: inputs) for layer_stride in layer_strides
        ]

        convolve_sequences, _ = nn.feed_forward_multi_layer(
            tf.expand_dims(sequences, axis=1),
            num_features, layer_sizes, FF_dropout, subnet_name,
            preactivation_fxns=preactivation_fxns,
            activation_fxns=activation_fxns,
        )

        return tf.squeeze(convolve_sequences, axis=1), set_initial_ind

    def _prepare_encoder_targets(
        self, sequenced_op_dict, draw_initial_ind, stride,
    ):

        # for each sequence type...
        for key, sequenced_op in sequenced_op_dict.items():
            # if it's an encoder_target...
            if key.startswith('encoder') and key.endswith('targets'):

                # reverse (to match reversal of inputs) and decimate
                _, get_targets_lengths = nn.sequences_tools(sequenced_op)
                reverse_targets = tf.reverse_sequence(
                    input=sequenced_op, seq_lengths=get_targets_lengths,
                    seq_axis=1, batch_axis=0)
                decimate_targets = reverse_targets[:, draw_initial_ind::stride, :]

                # NB: this overwrites the targets with their decimated version
                sequenced_op_dict[key] = decimate_targets

        return sequenced_op_dict

    def _decode_training_tokens(
        self, sequenced_op_dict, final_encoder_states, subnet_params,
        FF_dropout, eos_id
    ):

        # ...
        get_desequenced_natural_params = self._output_net(
            final_encoder_states[-1].h,
            self.layer_sizes['decoder_rnn'][-1],
            self.layer_sizes['decoder_projection'],
            subnet_params.data_manifests['decoder_targets'].num_features,
            FF_dropout, final_preactivation=self._t2t_final_affine_fxn
        )

        # resequence
        get_targets = sequenced_op_dict['decoder_targets']
        index_elements, _ = nn.sequences_tools(get_targets)
        sequenced_op_dict['decoder_natural_params'] = tf.scatter_nd(
            index_elements, get_desequenced_natural_params,
            [tf.shape(get_targets)[0], tf.shape(get_targets)[1],
             tf.shape(get_desequenced_natural_params)[-1]]
        )

        return sequenced_op_dict

    def _decode_training_sequences(
        self, sequenced_op_dict, final_encoder_states, subnet_params,
        FF_dropout, eos_id
    ):
        '''
        Initialize an RNN at the get_final_encoder_state, run on the targets,
        right-shifted by one (so the first entry is an EOS), and collect up the
        desequenced outputs, get_decoder_natural_params, for all time steps.
        NB that the targets are *not* used here to take the sample average in
        the cross entropy <-log(q)>_p. They are necessary nevertheless in order
        to evaluate the decoder natural params themselves (and therefore q in
        the cross entropy), which depend at each time step on the previous word
        in the actual target sequence.  See Eq'n (4) and surrounding disussion
        in "Machine Translation of Cortical Activity to Text with an
        Encoder-Decoder Framework."

        This function additionally desequences the targets.
        '''

        # init
        get_targets = sequenced_op_dict['decoder_targets']
        index_sequences_elements, get_sequences_lengths = nn.sequences_tools(
            get_targets)
        Nsequences = common_layers.shape_list(final_encoder_states[-1].h)[0]
        initial_ids = tf.fill([Nsequences, 1, 1], eos_id)

        # embed input sequences; pass thru RNN; de-sequence outputs
        targ_shapes = common_layers.shape_list(get_targets)[0:2] + [
            subnet_params.data_manifests['decoder_targets'].num_features]
        prev_targets = tf.concat((initial_ids, get_targets[:, :-1, :]), axis=1)
        embed_output_sequences = self._sequence_embed(
            prev_targets, targ_shapes, index_sequences_elements,
            self.layer_sizes['decoder_embedding'], FF_dropout,
            'decoder_embedding',
            preactivation_fxn=self._t2t_embedding_affine_fxn)
        get_RNN_outputs, _ = nn.LSTM_rnn(
            tf.cast(embed_output_sequences, tf.float32), get_sequences_lengths,
            self.layer_sizes['decoder_rnn'], self.RNN_dropout, 'decoder_rnn',
            initial_state=final_encoder_states)
        desequence_RNN_outputs = tf.gather_nd(
            get_RNN_outputs, index_sequences_elements)
        get_desequenced_natural_params = self._output_net(
            desequence_RNN_outputs,
            self.layer_sizes['decoder_rnn'][-1],
            self.layer_sizes['decoder_projection'],
            subnet_params.data_manifests['decoder_targets'].num_features,
            FF_dropout,
            final_preactivation=self._t2t_final_affine_fxn
        )

        # resequence
        index_elements, _ = nn.sequences_tools(get_targets)
        sequenced_op_dict['decoder_natural_params'] = tf.scatter_nd(
            index_elements, get_desequenced_natural_params,
            [tf.shape(get_targets)[0], tf.shape(get_targets)[1],
             tf.shape(get_desequenced_natural_params)[-1]]
        )

        return sequenced_op_dict

    @staticmethod
    def _set_final_nonpads(ids, nonpad_value, pad_value):
        # NB: THIS ASSUMES THAT 0 IS THE PADDING VALUE!  Ideally this method
        #  would be more flexible, but scatter_nd inits its tensor to zeros.
        #  Instead you would first create a tensor of the right shape, and then
        #  use scatter_nd_update, but that's hard....

        # index_nonpads is (Nnonpads x 2), 2 b/c row and col index
        index_nonpads = tf.cast(
            tf.compat.v1.where(tf.not_equal(ids[:, :, -1], 0)), tf.int32)

        # make_nonpads_updates is (Nnonpads x 1)
        make_nonpads_updates = tf.expand_dims(
            tf.fill(tf.shape(input=index_nonpads)[0:1], nonpad_value), axis=1)

        # terminal_ids has the shape of one slice of ids
        # SEE nn.tf_sentence_to_word_ids, sparse_tensor_to_dense?????
        #hold_terminal_ids = tf.placeholder(
        #    'int32', shape=common_layers.shape_list(ids[:, :, -1, None]))
        #pad_ids = tf.Variable(pad_value, dtype=hold_terminal_ids.dtype)
        #pad_ids = tf.assign(pad_iterminal_ids = tf.scatter_nd_update(
        #    ds, hold_terminal_ids, validate_shape=False)
        #terminal_ids = tf.scatter_nd_update(
        #    pad_ids, index_nonpads, make_nonpads_updates)
        # NB: THIS *ASSUMES* THAT pad_index = 0
        terminal_ids = tf.scatter_nd(
            index_nonpads, make_nonpads_updates,
            common_layers.shape_list(ids[:, :, -1, None]))

        # Throw out the original last slice and concat on the new terminal_ids.
        #  Also eliminate the *first* entries, which are *always* <EOS>: This
        #  beam_search assumes its first output to be the zeroth input--forcing
        #  you to discard this output explicitly.
        return tf.concat((ids[:, :, 1:-1], terminal_ids), axis=2)

    def _sequence_embed(
        self, get_sequences, sequences_shapes, index_sequences_elements,
        emb_layer_sizes, FF_dropout, subnet_name, preactivation_fxn=None
    ):
        '''
        To embed sequence data, you have first to de-sequence them, from
            [N_cases x max_sequence_length x len(single token vector)]
        to
            [\sum_i^N_cases sequence_length_i x len(single token vector)].
        Then you "embed" into a matrix of size
            [\sum_i^N_cases sequence_length_i x N_embedding_dims].
        Finally, you re-sequence into
            [N_cases x max_sequence_length x N_embedding_dims].

        Note that the outputs of an RNN with this input are in a sense
        also de-sequenced, since they have size
            [N_cases x N_hiddens].
        '''

        # Ns
        if preactivation_fxn is None:
            preactivation_fxn = self._vanilla_affine_fxn
        Ninputs = sequences_shapes[2]
        # NB: there's a bug here: this hack will fail if there is no input
        # embedding! (emb_layer_sizes=[]).  In that case Ninputs = Nclasses
        # which will conflict w/the actual input size, sc. 1.
        ###
        desequence = tf.gather_nd(get_sequences, index_sequences_elements)
        embed_desequenced, Ninputs = nn.feed_forward_multi_layer(
            desequence, Ninputs, emb_layer_sizes, FF_dropout, subnet_name,
            preactivation_fxns=[preactivation_fxn]*len(emb_layer_sizes))
        resequence_embedded_sequences = tf.scatter_nd(
            index_sequences_elements, embed_desequenced, tf.cast(
                [sequences_shapes[0], sequences_shapes[1], Ninputs], tf.int32))
        resequence_embedded_sequences.set_shape([None, None, Ninputs])
        #  https://github.com/tensorflow/tensorflow/issues/2938

        return resequence_embedded_sequences

    # CURRENTLY DEPRECATED
    def _sequence_dilate(
        self, sequences, emb_layer_sizes, FF_dropout, emb_strings,
        kernel_size=2
    ):

        ######
        # Use emb_strings to name the layers....
        ######
        z = nn.TemporalConvNet(emb_layer_sizes, kernel_size, FF_dropout)(
            sequences, training=True)
        index_sequences_elements, get_sequences_lengths = nn.sequences_tools(z)

        return z, get_sequences_lengths, index_sequences_elements

    def _output_net(
        self, get_activations, num_input_features, layer_sizes,
        num_output_features, FF_dropout, final_preactivation=None,
        subnet_name='decoder_projection'
    ):
        '''
        Just a little wrapper for feed_forward_multi_layer.  It builds an MLP
        followed by affine transformation--the natural params for some
        exponential-family distribution.
        '''
        Nlayers = len(layer_sizes)
        if final_preactivation is None:
            final_preactivation = self._vanilla_final_affine_fxn
        get_natural_params, _ = nn.feed_forward_multi_layer(
            get_activations, num_input_features,
            layer_sizes+[num_output_features], FF_dropout, subnet_name,
            preactivation_fxns=[self._vanilla_affine_fxn]*Nlayers+[
                final_preactivation],
            activation_fxns=[tf.nn.relu]*Nlayers+[lambda xx, name: xx]
        )
        return get_natural_params

    @_transpose_annotator(False)
    def _vanilla_affine_fxn(self, inputs, Nin, Nout):
        return nn.tf_matmul_wrapper(inputs, Nin, Nout, stiffness=self.stiffness)

    @_transpose_annotator(False)
    def _t2t_embedding_affine_fxn(self, inputs, Nin, Nout):
        return nn.tf_matmul_wrapper(
            inputs, Nin, Nout, stiffness=self.stiffness,
            num_shards=self.num_seq2seq_shards,
            USE_BIASES=self.BIAS_DECODER_OUTPUTS
        )

    @_transpose_annotator(True)
    def _vanilla_final_affine_fxn(self, inputs, Nin, Nout):
        return nn.tf_matmul_wrapper(
            inputs, Nin, Nout, stiffness=self.stiffness,
            transpose_b=True, USE_BIASES=True)

    @_transpose_annotator(True)
    def _t2t_final_affine_fxn(self, inputs, Nin, Nout):
        return nn.tf_matmul_wrapper(
            inputs, Nin, Nout, stiffness=self.stiffness,
            transpose_b=True, num_shards=self.num_seq2seq_shards,
            USE_BIASES=self.BIAS_DECODER_OUTPUTS)

    def _write_assessments(self, sequenced_op_dict, params, plotting_fxn):

        # One can request any sequenced_op via the assessment_op_set--just make
        #  sure it's also in the all_assessment_ops list
        for op_key, sequenced_op in sequenced_op_dict.items():
            # if requested...
            if op_key in self.assessment_op_set:
                # ...identify this operation for returning to the user
                sequenced_op_dict[op_key] = tf.identity(
                    sequenced_op, name='assess_' + op_key
                )

        # for sequences of categorical data
        for key, data_manifest in params.data_manifests.items():
            if '_targets' in key:

                # <-log[q(outputs_d|inputs)]>_p(outputs_d,inputs),
                # <-log[q(outputs_e|inputs)]>_p(outputs_e,inputs)
                compute_cross_entropy = nn.cross_entropy(
                    key, data_manifest, sequenced_op_dict
                )
                ce_key = nn.swap(key, 'cross_entropy')
                if ce_key in self.assessment_op_set:
                    compute_cross_entropy = tf.identity(
                        compute_cross_entropy, name='assess_' + ce_key
                    )

                # write cross_entropy to tb, whether or not it was requested
                self.summary_op_set.add(ce_key)
                tf.compat.v1.summary.scalar(
                    'summarize_' + ce_key, np.log2(np.e)*compute_cross_entropy
                )

                # make some extra assessments of categorical data
                if data_manifest.distribution == 'categorical':
                    self._write_categorical_assessments(
                        key, sequenced_op_dict, params, plotting_fxn
                    )

    def _write_categorical_assessments(
        self, key, sequenced_op_dict, params, plotting_fxn
    ):

        # gather some useful tensors
        sequenced_targets = sequenced_op_dict[key]
        sequenced_natural_params = sequenced_op_dict[nn.swap(key, 'natural_params')]
        index_targets, _ = nn.sequences_tools(sequenced_targets)
        desequenced_targets = tf.cast(
            tf.gather_nd(sequenced_targets, index_targets), tf.int32
        )
        desequenced_natural_params = tf.gather_nd(
            sequenced_natural_params, index_targets
        )

        # write assessents
        assess_accuracies, predict_top_k_inds = self._write_accuracies(
            key, desequenced_targets, desequenced_natural_params
        )
        self._write_word_error_rates(key, sequenced_op_dict, params)
        confusions = self._write_confusions(
            key, desequenced_targets, desequenced_natural_params,
            predict_top_k_inds, params, plotting_fxn
        )
        self._write_frequency_normalized_stats(key, confusions)
        self._write_calibration(
            key, desequenced_natural_params, assess_accuracies, params
        )

    def _write_accuracies(
        self, key, desequenced_targets, desequenced_natural_params
    ):
        # top-k accuracy
        # \sum_i=1^k{<\delta{outputs_d - argmax_a[q(a|inputs)]}>_p(outputs_d,inputs)}
        _, predict_top_k_inds = tf.nn.top_k(
            desequenced_natural_params, k=self.k_for_top_k_accuracy
        )
        predict_top_k_inds = tf.identity(
            predict_top_k_inds, name='assess_%s' % nn.swap(key, 'top_k_inds')
        )
        assess_accuracies = tf.cast(
            tf.equal(predict_top_k_inds, desequenced_targets), tf.float32
        )
        average_accuracies = tf.reduce_mean(assess_accuracies, axis=0)
        if nn.swap(key, 'top_%i_accuracy' % self.k_for_top_k_accuracy) in self.summary_op_set:
            tf.compat.v1.summary.scalar(
                'summarize_%s' % nn.swap(
                    key, 'top_%i_accuracy' % self.k_for_top_k_accuracy
                ),
                tf.reduce_sum(average_accuracies)
            )

        # <\delta{outputs_d - argmax_a[q(a|inputs)]}>_p(outputs_d,inputs)
        assess_average_accuracy = tf.gather(
            average_accuracies, 0, name='assess_%s' % nn.swap(key, 'accuracy')
        )
        if nn.swap(key, 'accuracy') in self.summary_op_set:
            tf.compat.v1.summary.scalar(
                'summarize_%s' % nn.swap(key, 'accuracy'), assess_average_accuracy
            )

        return assess_accuracies, predict_top_k_inds

    def _write_word_error_rates(self, key, sequenced_op_dict, params):

        # the tensors required to compute a word error rate
        sequenced_outputs = sequenced_op_dict[nn.swap(key, 'outputs')]
        sequenced_beam_targets = sequenced_op_dict[nn.swap(key, 'beam_targets')]
        sequence_log_probs = tf.identity(
            sequenced_op_dict[nn.swap(key, 'sequence_log_probs')],
            name='assess_%s' % nn.swap(key, 'sequence_log_probs')
        )

        # minimum normalized edit distance between sequences of words
        targets_list = params.data_manifests[key].get_feature_list()
        eos_id = (
            targets_list.index(self.EOS_token)
            if self.EOS_token in targets_list else -1
        )
        get_word_error_rates = nn.tf_expected_word_error_rates(
            sequenced_beam_targets, sequenced_outputs, sequence_log_probs,
            EXCLUDE_EOS=True, eos_id=eos_id
        )
        assess_word_error_rate = tf.reduce_mean(
            get_word_error_rates, name='assess_%s' % nn.swap(key, 'word_error_rate')
        )
        ######
        # FIX ME
        # tf.compat.v1.get_collection('my_collection')
        # tf.compat.v1.add_to_collection('my_collection', assess_word_error_rate)
        # EMA = tf.train.ExponentialMovingAverage(decay=0.9)
        # assess_word_error_rate = EMA.apply(tf.compat.v1.get_collection('my_collection'))
        ######

        if nn.swap(key, 'word_error_rate') in self.summary_op_set:
            tf.compat.v1.summary.scalar(
                'summarize_%s' % nn.swap(key, 'word_error_rate'),
                assess_word_error_rate
            )

    def _write_confusions(
        self, key, desequenced_targets, desequenced_natural_params,
        predict_top_k_inds, params, plotting_fxn
    ):
        # tf's confusion matrix wants predictions, not probs.  Therefore,
        #  you *prefer* to use your own version, using output *probabilities*.
        num_target_features = params.data_manifests[key].num_features
        if self.PROBABILISTIC_CONFUSIONS:
            # the kind of confusion matrix, via conditional probabilities
            qvec_samples = tf.nn.softmax(desequenced_natural_params)
            xpct_pvec_qvec_unnorm = tf.scatter_nd(
                desequenced_targets, qvec_samples,
                tf.constant([num_target_features]*2, dtype=tf.int32))
            xpct_pvec_unnorm = tf.reduce_sum(
                xpct_pvec_qvec_unnorm, axis=1, keepdims=True
            )
            confusions = tf.divide(
                xpct_pvec_qvec_unnorm, xpct_pvec_unnorm,
                name='assess_%s' % nn.swap(key, 'confusions')
            )
        else:
            # get confusions and supply a name to the op
            confusions = tf.math.confusion_matrix(
                labels=tf.reshape(desequenced_targets, [-1]),
                predictions=predict_top_k_inds[:, 0],
                num_classes=num_target_features)
            confusions = tf.identity(
                confusions, name='assess_%s' % nn.swap(key, 'confusions')
            )

        image_key = nn.swap(key, 'confusions_image')
        if image_key in self.summary_op_set:
            tf.compat.v1.summary.image(
                'summarize_%s' % image_key,
                plotting_fxn(
                    confusions, params.data_manifests[key].get_feature_list()
                )
            )

        return confusions

    def _write_frequency_normalized_stats(self, key, confusions):

        # EXPECTED frequency-normalized accuracy
        xpct_key = nn.swap(key, 'xpct_normalized_accuracy')
        target_frequencies = tf.reduce_sum(confusions, axis=1)
        where_targets = tf.cast(
            tf.compat.v1.where(target_frequencies > 0), tf.int32
        )
        frequency_normalized_accuracies = tf.divide(
            tf.gather(tf.linalg.diag_part(confusions), where_targets),
            tf.gather(target_frequencies, where_targets))
        assess_xpct_frequency_normalized_accuracy = tf.reduce_mean(
            frequency_normalized_accuracies, name='assess_%s' % xpct_key
        )
        if xpct_key in self.summary_op_set:
            tf.compat.v1.summary.scalar(
                'summarize_%s' % xpct_key,
                assess_xpct_frequency_normalized_accuracy
            )

        # VARIANCE of the frequency-normalized accuracy
        vrnc_key = nn.swap(key, 'vrnc_normalized_accuracy')
        assess_vrnc_frequency_normalized_accuracy = tf.reduce_mean(
            tf.math.squared_difference(
                frequency_normalized_accuracies,
                assess_xpct_frequency_normalized_accuracy
            ),
            name='assess_%s' % vrnc_key)
        if vrnc_key in self.summary_op_set:
            tf.compat.v1.summary.scalar(
                'summarize_%s' % vrnc_key,
                assess_vrnc_frequency_normalized_accuracy
            )

    def _write_calibration(
        self, key, desequenced_natural_params, assess_accuracies, params
    ):

        # the average entropy of the *output distribution*
        s_key = nn.swap(key, 'entropy')
        if s_key in self.summary_op_set:
            C = tf.reduce_logsumexp(desequenced_natural_params, axis=1)
            decoder_probs = tf.nn.softmax(desequenced_natural_params)
            assess_entropies = C - tf.reduce_sum(tf.multiply(
                decoder_probs, desequenced_natural_params), axis=1)
            average_entropy = tf.reduce_mean(assess_entropies)
            tf.compat.v1.summary.scalar(
                'summarize_%s' % s_key, np.log2(np.e)*average_entropy
            )
        else:
            return

        # and does it correlate with accuracy?
        s_key = nn.swap(key, 'calibration')
        if s_key in self.summary_op_set:
            acc_entropy_corr = tfp.stats.correlation(
                assess_accuracies[:, 0], assess_entropies, event_axis=None)
            tf.compat.v1.summary.scalar(
                'summarize_%s' % s_key, acc_entropy_corr
            )

        # also *look* at the relationship
        s_key = nn.swap(key, 'calibration_image')
        if s_key in self.summary_op_set:
            num_target_features = params.data_manifests[key].num_features
            tf.compat.v1.summary.image(s_key, dual_violin_plot(
                assess_entropies, assess_accuracies[:, 0], [0, 1],
                x_axis_label='correctness', y_axis_label='entropy',
                ymin=0.0, ymax=np.log2(num_target_features)
            ))

    def _assess(
        self, sess, assessment_struct, epoch, assessment_step,
        decoder_targets_list, data_partition
    ):
        ########
        # add functinality for encoder categorical data?
        ########

        # The summaries you wish to make for tensorboard.  You need a deep copy
        #  because you intend to alter the set on a temporary basis.
        summary_op_set = copy.copy(self.summary_op_set)
        if len(decoder_targets_list) > 100:
            summary_op_set.discard('decoder_confusions_image')
        if (epoch % 10 != 0):
            summary_op_set.discard('decoder_confusions_image')
            summary_op_set.discard('decoder_calibration_image')

        # The assessments you wish to make for printing or returning.  Convert
        #  to a list to ensure the order is fixed
        assessment_op_list = list(self.assessment_op_set)

        # ...initialize the session with training/validation data
        sess.run(assessment_struct.initializer)

        # ...execute all summaries and assessments
        (summaries, assessments, subnet_id) = sess.run((
            [sess.graph.get_operation_by_name('summarize_' + summary_op).outputs[0]
             for summary_op in summary_op_set],
            [sess.graph.get_operation_by_name('assess_' + assessment_op).outputs[0]
             for assessment_op in assessment_op_list],
            sess.graph.get_operation_by_name('identify_subnet_id').outputs[0],
            # sess.graph.get_operation_by_name('seq2seq/case/identify_initial_ind').outputs[0],
         ))

        # update the assessment_struct with the assessments
        for field, assessment in zip(assessment_op_list, assessments):
            setattr(assessment_struct, field, assessment)

        # if there's a writer...
        if assessment_struct.writer:
            # ...write to tensorboard
            for summary in summaries:
                assessment_struct.writer.add_summary(summary, epoch)
            assessment_struct.writer.flush()

            if 'decoder_accuracy' in self.assessment_op_set:
                # ...and to the screen
                print("step %2d: %10s decoder accuracy (%i) = %.2g" % (
                    epoch, data_partition, subnet_id,
                    assessment_struct.decoder_accuracy
                ))
                assessment_struct.decoder_accuracies[
                    assessment_step] = assessment_struct.decoder_accuracy
            if 'decoder_word_error_rate' in self.assessment_op_set:
                assessment_struct.decoder_word_error_rates[
                    assessment_step] = assessment_struct.decoder_word_error_rate

            # print some assessments
            if not self.TARGETS_ARE_SEQUENCES:
                # Non-sequence references/hypotheses are based on a fake_beam,
                #  so we have to follow its lead and (re)build a unique tokens
                #  list
                decoder_targets_list = nn.targets_to_tokens(
                    decoder_targets_list, self.pad_token)

            if (
                'decoder_sequence_log_probs' in self.assessment_op_set and
                'decoder_outputs' in self.assessment_op_set and
                'decoder_beam_targets' in self.assessment_op_set
            ):
                on_clr = 'on_yellow' if data_partition == 'training' else 'on_cyan'

                # references
                sequenced_decoder_target = self.target_inds_to_sequences(
                    assessment_struct.decoder_beam_targets, decoder_targets_list
                )[0]
                cprint(
                    'example %s reference:' % data_partition, on_color=on_clr
                )
                cprint('\t' + sequenced_decoder_target, on_color='on_red')

                # hypotheses
                sequenced_decoder_outputs = self.target_inds_to_sequences(
                    assessment_struct.decoder_outputs,
                    decoder_targets_list
                )
                decoder_sequence_log_probs = assessment_struct.decoder_sequence_log_probs[0]
                log_probs = decoder_sequence_log_probs - logsumexp(
                    decoder_sequence_log_probs)
                probs = np.exp(log_probs)
                cprint(
                    'example ' + data_partition + ' hypothesis:',
                    on_color=on_clr
                )
                for ind in range(self.beam_width):
                    cprint('%.2f\t' % probs[ind] + sequenced_decoder_outputs[ind],
                           on_color='on_green')
                print('')

                # print *all* validation hypotheses and references
                num_examples = assessment_struct.decoder_outputs.shape[0]
                for iExample in range(num_examples):
                    ref = self.target_inds_to_sequences(
                        assessment_struct.decoder_beam_targets,
                        decoder_targets_list, iExample)[0]
                    hyp = self.target_inds_to_sequences(
                        assessment_struct.decoder_outputs,
                        decoder_targets_list, iExample)[0]
                    cprint('{0:60} {1}'.format(ref, hyp), on_color='on_cyan')
                    if iExample > 50:
                        break
                print('')

                # debugging: print images....
                if data_partition == 'training':
                    clear_output(wait=True)

        return assessment_struct

    def _batch_and_split_data(
        self, subnets_params, num_GPUs, data_partition='training'
    ):
        # remove any device specifications for the input data
        with tf.device(None):

            # create an iterator across batches from the tf_records
            dataset = self._tf_records_to_dataset(
                subnets_params, data_partition, self.N_cases,
                self.num_training_shards_to_discard
            )
            iterator = tf.compat.v1.data.make_initializable_iterator(dataset)

            # get the next batch and break into sequences and subnet_id dicts
            GPU_op_dict = iterator.get_next()
            CPU_keys = ['subnet_id']
            CPU_op_dict = {key: GPU_op_dict.pop(key) for key in CPU_keys}

            # split data for processing across GPUs.
            batch_size = tf.shape(GPU_op_dict['decoder_targets'])[0]
            final_index = batch_size - tf.math.mod(batch_size, num_GPUs)
            GPU_split_op_dict = {
                key: tf.split(
                    axis=0, num_or_size_splits=num_GPUs,
                    value=batch_sequence_data[:final_index]
                ) for key, batch_sequence_data in GPU_op_dict.items()
            }

            return GPU_split_op_dict, CPU_op_dict, iterator.initializer

    def _generate_oneshot_datasets(self, assessment_params, num_epochs):
        # use as many training as *validation* samples
        num_assessment_examples = sum(
            [sum(1 for _ in tf.compat.v1.python_io.tf_record_iterator(
                assessment_params.tf_record_partial_path.format(block_id)))
             for block_id in assessment_params.block_ids['validation']])

        # for each data type that you want to assess, create a dataset
        assessments = dict.fromkeys(self.assessment_partitions)
        for i, data_partition in enumerate(assessments):
            dataset = self._tf_records_to_dataset(
                [assessment_params], data_partition, num_assessment_examples
            )
            if i == 0:
                # create just one iterator---from *any* dataset's types
                #  and shapes, since they're all the same
                iterator = tf.compat.v1.data.Iterator.from_structure(
                    tf.compat.v1.data.get_output_types(dataset),
                    tf.compat.v1.data.get_output_shapes(dataset)
                )
            assessments[data_partition] = self._initialize_assessment_struct(
                iterator.make_initializer(dataset), data_partition, num_epochs)

        # get the all data and break into sequences and subnet_id dicts
        GPU_op_dict = iterator.get_next()
        CPU_keys = ['subnet_id']
        CPU_op_dict = {key: GPU_op_dict.pop(key) for key in CPU_keys}

        return GPU_op_dict, CPU_op_dict, assessments

    @staticmethod
    def _standard_indexer(sequences):
        (index_sequences_elements, get_sequences_lengths) = nn.sequences_tools(
            sequences)
        max_length = tf.reduce_max(get_sequences_lengths)
        # "you should use something longer than max_sequences_lengths!"
        return index_sequences_elements, max_length

    def target_inds_to_sequences(self, hypotheses, targets_list, iExample=0):
        predicted_tokens = [
            ''.join([targets_list[ind] for ind in hypothesis]).replace(
                '_', ' ').replace(self.pad_token, '').replace(
                self.EOS_token, '').rstrip()
            for hypothesis in hypotheses[iExample]
        ]
        return predicted_tokens

    def _tf_records_to_dataset(
        self, subnets_params, data_partition, num_cases,
        num_shards_to_discard=0, DROP_REMAINDER=False
    ):
        '''
        Load, shuffle, batch and pad, and concatentate across subnets (for
        parallel transfer learning) all the data.
        '''

        # accumulate datasets, one for each subnetwork
        dataset_list = []
        for subnet_params in subnets_params:
            dataset = tf.data.TFRecordDataset([
                subnet_params.tf_record_partial_path.format(block_id)
                for block_id in subnet_params.block_ids[data_partition]]
            )
            dataset = dataset.map(
                lambda example_proto: tfh.parse_protobuf_seq2seq_example(
                    example_proto, subnet_params.data_manifests
                ),
                num_parallel_calls=tf.data.experimental.AUTOTUNE
            )
            #########
            # Insane tensorflow bug: "num_parallel_calls" cannot be moved to
            #  the preceding line (after the comma), and results in extremely
            #  erratic behavior (especially in conjunction with Jupyter)
            #########

            # filter data to include or exclude only specified decoder targets?
            decoder_targets_list = subnet_params.data_manifests[
                'decoder_targets'].get_feature_list()
            target_filter = TargetFilter(
                decoder_targets_list, subnet_params.target_specs,
                data_partition
            )
            dataset = target_filter.filter_dataset(dataset)

            # # filter out words not in the decoder_targets_list
            # ######
            # # FIX ME
            # if False:  # not self.TARGETS_ARE_SEQUENCES:
            #     OOV_id = (decoder_targets_list.index(self.OOV_token)
            #               if self.OOV_token in decoder_targets_list else -1)
            #     dataset = dataset.filter(
            #         lambda encoder_input, decoder_target, encoder_target, s_id:
            #             tf.not_equal(decoder_target[0], OOV_id))
            # ######

            # discard some of the data?; shuffle; batch (evening out w/padding)
            if num_shards_to_discard > 0:
                dataset = dataset.shard(num_shards_to_discard+1, 0)
            dataset = dataset.shuffle(buffer_size=35000)  # > greatest
            dataset = dataset.padded_batch(
                num_cases,
                padded_shapes=tf.compat.v1.data.get_output_shapes(dataset),
                padding_values={
                    key: data_manifest.padding_value
                    for key, data_manifest in subnet_params.data_manifests.items()
                },
                drop_remainder=DROP_REMAINDER
            )

            # add id for "proprietary" parts of network under transfer learning
            dataset = dataset.map(
                lambda batch_of_protos_dict: {
                    **batch_of_protos_dict,
                    'subnet_id': tf.constant(
                        subnet_params.subnet_id, dtype=tf.int32)
                }
            )
            dataset_list.append(dataset)

        # (randomly) interleave (sub-)batches w/o throwing anything away
        dataset = reduce(
            lambda set_a, set_b: set_a.concatenate(set_b), dataset_list
        )
        dataset = dataset.shuffle(buffer_size=3000)
        ######
        # Since your parse_protobuf_seq2seq_example isn't doing much, the
        #  overhead associated with just scheduling the dataset.map will
        #  dominate the cost of applying it.  Therefore, tensorflow
        #  recommends batching first, and applying a vectorized version of
        #  parse_protobuf_seq2seq_example.  But you shuffle first.....
        ######
        dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE) #num_cases)

        return dataset

    def compute_learning_rate(self, subnets_params, N_cases_total=None):
        if not N_cases_total:
            data_graph = tf.Graph()
            with data_graph.as_default():
                dataset = tf.data.TFRecordDataset([
                    subnet_params.tf_record_partial_path.format(block_id)
                    for subnet_params in subnets_params
                    for block_id in subnet_params.block_ids['training']
                ])
                count_records = dataset.reduce(0, lambda x, _: x + 1)
                N_cases_total = tf.compat.v1.Session().run(count_records)
        learning_rate = self.temperature/N_cases_total
        print('learning rate is %f' % learning_rate)

        return learning_rate

    def restore_and_assess(
        self, subnets_params, restore_epoch, WRITE=True, **graph_kwargs
    ):

        ######
        # This code is redundant with fit above....
        # You *could* just construct the GraphBuilder once in the constructor
        assessment_subnet_params = subnets_params[-1]
        decoder_targets_list = assessment_subnet_params.data_manifests[
            'decoder_targets'].get_feature_list()

        def assessment_data_fxn(num_epochs):
            (data_op_tuple, misc_op_tuple, assessments
             ) = self._generate_oneshot_datasets(
                assessment_subnet_params, num_epochs
            )

            if not WRITE:
                for assessment in assessments.values():
                    assessment.writer = None

            return data_op_tuple, misc_op_tuple, assessments

        @tfmpl.figure_tensor
        def plotting_fxn(confusions, axis_labels):
            fig = toolbox.draw_confusion_matrix(
                confusions, axis_labels, (12, 12))
            return fig

        def assessment_net_builder(GPU_op_dict, CPU_op_dict):
            return self._build_assessment_net(
                GPU_op_dict, CPU_op_dict, assessment_subnet_params,
                self._standard_indexer, plotting_fxn
            )

        def assessor(sess, assessment_struct, epoch, assessment_step, data_partition):
            return self._assess(
                sess, assessment_struct, epoch, assessment_step,
                decoder_targets_list, data_partition)
        ######

        # (re-)build the assessment graph and restore its params from the ckpt
        graph_builder = tfh.GraphBuilder(
            None, assessment_data_fxn, None, assessment_net_builder, None,
            assessor, self.checkpoint_path, restore_epoch, restore_epoch-1,
            EMA_decay=self.EMA_decay, assessment_GPU=self.assessment_GPU,
            **graph_kwargs
        )
        return graph_builder.assess()

    def get_weights_as_numpy_array(self, tensor_name, restore_epoch):

        # use the tensorflow checkpoint reader
        this_checkpoint = self.checkpoint_path + '-%i' % restore_epoch
        reader = pywrap_tensorflow.NewCheckpointReader(this_checkpoint)
        return reader.get_tensor(tensor_name)

    def _compute_guessable_class_indices(self, get_targets, subnet_params):
        # Not quite right, but easier to implement: construct a dictionary of
        #  size num_guessable_classes, then add in the words actually in the
        #  target sentences. Thus, the dictionary size will generally differ
        #  across sentences....

        # Ns
        num_words_avg = 7
        num_cases = tf.shape(input=get_targets)[0]
        num_decoder_target_features = subnet_params.data_manifests[
            'decoder_targets'].num_features

        # randomly generate the "extra"--incorrect but guessable--classes
        make_extra_classes = tf.tile(tf.expand_dims(tf.random.shuffle(tf.range(
            num_decoder_target_features))[
                :(self.num_guessable_classes-num_words_avg)
            ], axis=0), (num_cases, 1))

        # first get a tensor of the guessable classes
        tile_all_classes = tf.tile(tf.expand_dims(
            tf.range(num_decoder_target_features), axis=0), (num_cases, 1))
        get_unused_classes_matrix = tf.sets.difference(
            tile_all_classes, get_targets[:, :, 0])
        get_used_classes_matrix = tf.sets.difference(
            tile_all_classes, get_unused_classes_matrix)
        get_guessable_classes = tf.sparse.to_dense(tf.sets.union(
            get_used_classes_matrix, make_extra_classes))

        # expand to beam_width
        get_guessable_classes = tf.reshape(tf.tile(tf.expand_dims(
            get_guessable_classes, axis=1), (1, self.beam_width, 1)),
            [num_cases*self.beam_width, -1])

        # now get the corresponding indices (for scattering)
        get_guessable_class_row_indices = tf.reshape(tf.tile(
            tf.expand_dims(tf.range(num_cases*self.beam_width), axis=1),
            (1, tf.shape(get_guessable_classes)[1])), [-1])
        get_guessable_class_col_indices = tf.reshape(
            get_guessable_classes, [-1])
        get_guessable_indices = tf.stack(
            (get_guessable_class_row_indices, get_guessable_class_col_indices),
            axis=1)

        return get_guessable_indices

    def _reduced_classes_hack(
        self, score_as_unnorm_log_probs, get_guessable_indices
    ):

        # Ns
        num_cases = tf.shape(score_as_unnorm_log_probs)[0]

        # thing
        get_guessable_updates = tf.gather_nd(
            score_as_unnorm_log_probs, get_guessable_indices)
        get_guessable_unnorm_log_probs = tf.scatter_nd(
            get_guessable_indices, get_guessable_updates,
            tf.shape(score_as_unnorm_log_probs))

        # the pad should not be guessable
        get_guessable_unnorm_log_probs = tf.concat(
            (tf.zeros([num_cases, 1]), get_guessable_unnorm_log_probs[:, 1:]),
            axis=1)

        # being log probs, they can't be left at 0, so we need to populate the
        #  log prob matrix for the classes we *don't* want to select from, too
        index_unguessable_unnorm_log_probs = tf.cast(
            tf.compat.v1.where(tf.equal(get_guessable_unnorm_log_probs, 0)), tf.int32)
        ###
        get_batch_min = tf.reduce_min(score_as_unnorm_log_probs)
        # This feels ugly--would be better, albeit more complicated, to use the
        #  row mins. On the other hand, you still have to do the weird thing of
        #  multiplying it by two or whatever....
        ###
        make_unguessable_updates = tf.fill(
            tf.shape(index_unguessable_unnorm_log_probs)[0:1], get_batch_min)
        get_unguessable_unnorm_log_probs = tf.scatter_nd(
            index_unguessable_unnorm_log_probs, make_unguessable_updates,
            tf.shape(score_as_unnorm_log_probs))

        # now add the two pieces together
        return get_guessable_unnorm_log_probs + get_unguessable_unnorm_log_probs


class TargetFilter:
    def __init__(self, unique_targets, target_specs, this_data_type):

        '''
        # Example:
        target_specs = {
            'validation': [
                ['this', 'was', 'easy', 'for', 'us'],
                ['they', 'often', 'go', 'out', 'in', 'the', 'evening'],
                ['i', 'honour', 'my', 'mum'],
                ['a', 'doctor', 'was', 'in', 'the', 'ambulance', 'with', 'the', 'patient'],
                ['we', 'are', 'open', 'every', 'monday', 'evening'],
                ['withdraw', 'only', 'as', 'much', 'money', 'as', 'you', 'need'],
                ['allow', 'each', 'child', 'to', 'have', 'an', 'ice', 'pop'],
                ['is', 'she', 'going', 'with', 'you']
            ]
        }
        '''

        # fixed
        data_types = {'training', 'validation'}

        # convert target_specs dictionary entries from word- to index-based
        # NB: PROBABLY NOT GENERAL ENOUGH to work w/non-word_sequence data
        self.target_specs = {key: [
            [unique_targets.index(w + '_') for w in target] + [1]
            for target in target_spec] for key, target_spec in target_specs.items()
        }

        # store for later use
        self.this_data_type = this_data_type
        self.other_data_type = (data_types - {this_data_type}).pop()

    def _test_special(self, fetch_target_indices, data_type):
        # Test if this tf_record target is among this dataset's target_specs.
        # NB that this function returns a (boolean) tf.tensor.
        TEST_SPECIAL = tf.constant(False)
        for target_indices in self.target_specs[data_type]:
            TEST_MATCH = tf.reduce_all(
                tf.linalg.diag_part(tf.equal(
                    fetch_target_indices,
                    np.array(target_indices, ndmin=2))
                ))
            TEST_SPECIAL = tf.logical_or(TEST_SPECIAL, TEST_MATCH)
        return TEST_SPECIAL

    def filter_dataset(self, dataset):
        if self.this_data_type in self.target_specs:
            return dataset.filter(
                lambda example_dict: self._test_special(
                    example_dict['decoder_targets'], self.this_data_type
                ))
        elif self.other_data_type in self.target_specs:
            return dataset.filter(
                lambda example_dict: self._test_special(
                    example_dict['decoder_targets'], self.other_data_type
                ))
        else:
            return dataset


def data_augmentor(sequenced_op_dict, keyword):
    ######
    # This has a bunch of values hard-coded in--including the booleans that
    #  control whether or not something happens.  At some future date you
    #  might generalize it.
    ######

    # temporally warp the encoder data
    if False:
        draw_stretch_factor = tf.random.uniform(
            [1], minval=0.4, maxval=1.5, dtype=tf.float32)[0]
        for key, sequenced_op in sequenced_op_dict:
            if keyword in key:
                sequenced_op_dict[key] = nn.tf_linear_interpolation(
                    sequenced_op, draw_stretch_factor, axis=1)

    # jitter the onset and offset of the encoder data
    if False:
        draw_jitters = tf.random.uniform(
            [2], minval=200, maxval=1000, dtype=tf.int32)
        for key, sequenced_op in sequenced_op_dict:
            if keyword in key:
                sequenced_op_dict[key] = sequenced_op[
                    :, draw_jitters[0]:-50, :]

    return sequenced_op_dict
