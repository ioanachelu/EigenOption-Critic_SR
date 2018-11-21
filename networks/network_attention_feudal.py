import tensorflow as tf
import tensorflow.contrib.layers as layers
from config_utility import gradient_summaries, huber_loss
import numpy as np
from networks.network_eigenoc import EignOCNetwork
import os
from online_clustering import OnlineCluster
from auxilary.lstm_model import SingleStepLSTM

def normalized_columns_initializer(std=1.0):
    def _initializer(shape, dtype=None, partition_info=None):
        out = np.random.randn(*shape).astype(np.float32)
        out *= std / np.sqrt(np.square(out).sum(axis=0, keepdims=True))
        return tf.constant(out)

    return _initializer

class AttentionFeudalNetwork(EignOCNetwork):
  def __init__(self, scope, config, action_size):
    self.goal_embedding_size = config.sf_layers[-1]
    super(AttentionFeudalNetwork, self).__init__(scope, config, action_size)
    if self.config.use_clustering:
      self.init_clustering()

  def build_network(self):
    with tf.variable_scope(self.scope):
      self.prob_of_random_goal = tf.Variable(self.config.initial_random_goal_prob, trainable=False, name="prob_of_random_goal", dtype=tf.float32)
      ## PLACEHOLDERS ##
      self.target_sf = tf.placeholder(shape=[None, self.config.sf_layers[-1]], dtype=tf.float32, name="target_SF")
      self.target_goal = tf.placeholder(shape=[None, self.goal_embedding_size], dtype=tf.float32, name="target_goal")
      self.prev_goals = tf.placeholder(shape=[None, None, self.goal_embedding_size], dtype=tf.float32, name="prev_goals")

      self.target_mix_return = tf.placeholder(shape=[None], dtype=tf.float32, name="target_mix_return")
      self.target_v_ext = tf.placeholder(shape=[None], dtype=tf.float32, name="target_v_ext")
      self.target_return = tf.placeholder(shape=[None], dtype=tf.float32, name="target_return")
      self.actions_placeholder = tf.placeholder(shape=[None], dtype=tf.int32, name="actions_placeholder")
      """Placeholder for the previous rewards"""
      self.prev_rewards = tf.placeholder(shape=[None], dtype=tf.float32, name="Prev_Rewards")
      self.prev_rewards_expanded = tf.expand_dims(self.prev_rewards, 1)
      """Placeholder for the previous actions"""
      self.prev_actions = tf.placeholder(shape=[None], dtype=tf.int32, name="Prev_Actions")
      self.prev_actions_onehot = tf.one_hot(self.prev_actions, self.action_size, dtype=tf.float32, name="Prev_Actions_OneHot")

      self.observation = tf.placeholder(
        shape=[None, self.nb_states],
        dtype=tf.float32, name="observation_placeholder")
      self.observation_image = tf.placeholder(
        shape=[None, self.config.input_size[0], self.config.input_size[1], 1],
        dtype=tf.float32, name="observation_image_placeholder")

      hidden = tf.concat([self.observation, self.prev_rewards_expanded], 1, name="Concatenated_input")
      # hidden = tf.concat([self.observation, self.prev_rewards_expanded, self.prev_actions_onehot], 1, name="Concatenated_input")
			#
      goal_clusters = tf.placeholder(shape=[self.config.nb_options,
                                                 self.goal_embedding_size],
                                          dtype=tf.float32,
                                          name="goal_clusters")
      self.goal_clusters = tf.nn.l2_normalize(goal_clusters, 1)
      ## -------------##

      self.image_summaries.append(
        tf.summary.image('observation', self.observation_image, max_outputs=30))

      ## ------ SR -------#
      with tf.variable_scope("succ_feat"):
        self.sf = layers.fully_connected(self.observation,
                                     num_outputs=self.goal_embedding_size,
                                     activation_fn=None,
                                     biases_initializer=None,
                                     scope="sf")

      ## -----------------#

      ## Manager goal ##
      with tf.variable_scope("option_manager_policy"):
        """The merged representation of the input"""

        self.manager_lstm = SingleStepLSTM(tf.expand_dims(hidden, [0]),
                                           self.goal_embedding_size,
                                           step_size=tf.shape(self.observation)[:1])
        goal_features = self.manager_lstm.output
        # goal_features = layers.fully_connected(self.observation,
        #                                        num_outputs=self.goal_embedding_size,
        #                                        activation_fn=None,
        #                                        scope="goal_features")

        goal_hat = layers.fully_connected(goal_features,
                                                    num_outputs=self.goal_embedding_size,
                                                    activation_fn=None,
                                                    scope="goal_hat")
        self.query_goal = self.l2_normalize(goal_hat, 1)

        self.query_content_match = tf.einsum('bj, ij -> bi', self.query_goal, self.goal_clusters, name="query_content_match")

        self.attention_weights = tf.nn.softmax(self.query_content_match, name="attention_weights")

        # self.max_g = tf.gather(self.goal_clusters, tf.squeeze(tf.multinomial(self.query_content_match, 1), 1))
        # self.max_g = tf.tile(self.goal_clusters[0][None, ...], [tf.shape(self.query_content_match)[0], 1])
        self.current_unnormalized_goal = tf.einsum('bi, ij -> bj', self.attention_weights, self.goal_clusters, name="unnormalized_g")

        self.max_g = tf.identity(self.l2_normalize(self.current_unnormalized_goal, 1), name="g")

        """Take the random option with probability self.random_option_prob"""
        self.local_random = tf.random_uniform(shape=[tf.shape(self.max_g)[0]], minval=0., maxval=1., dtype=tf.float32, name="rand_goals")

        random_goal_sampling = tf.distributions.Categorical(probs=[1/self.config.nb_options for _ in range(self.config.nb_options)])
        self.which_goal = random_goal_sampling.sample(tf.shape(self.max_g)[0])
        self.random_g = tf.gather(self.goal_clusters, self.which_goal)

        self.random_goal_cond = self.local_random > self.prob_of_random_goal
        # self.random_g = tf.random_normal(shape=tf.shape(self.max_g))

        self.g = tf.where(self.random_goal_cond, self.max_g, self.random_g, name="current_goal")

        self.prev_goals_rand = tf.where(self.random_goal_cond, self.prev_goals, tf.tile(tf.expand_dims(self.g, 1), [1, self.config.c, 1]))

      with tf.variable_scope("option_manager_value_ext"):
        # extrinsic_features = layers.fully_connected(goal_features,
        #                                        num_outputs=self.goal_embedding_size,
        #                                        activation_fn=None,
        #                                        scope="extrinsic_features")
        v_ext = layers.fully_connected(goal_features,
                                               num_outputs=1,
                                               activation_fn=None,
                                               scope="v_ext")
        self.v_ext = tf.squeeze(v_ext, 1)

      with tf.variable_scope("option_worker_features"):
        self.worker_lstm = SingleStepLSTM(tf.expand_dims(hidden, [0]),
                                          size=self.action_size * self.goal_embedding_size,
                                          step_size=tf.shape(self.observation)[:1])
        intrinsic_features = self.worker_lstm.output

        intrinsic_features = layers.fully_connected(intrinsic_features,
                                                num_outputs=self.action_size * self.goal_embedding_size,
                                                activation_fn=None,
                                                scope="intrinsic_features")
        policy_features = tf.reshape(intrinsic_features, [-1, self.action_size,
                                                           self.goal_embedding_size],
                                          name="policy_features")
        value_features = tf.identity(intrinsic_features, name="value_features")

      cut_g = tf.stop_gradient(self.g)
      cut_g = tf.expand_dims(cut_g, 1)
      self.g_stack = tf.concat([self.prev_goals_rand, cut_g], 1)
      self.last_c_g = self.g_stack[:, 1:]
      self.g_sum = tf.reduce_sum(self.g_stack, 1)

      with tf.variable_scope("option_worker_value_mix"):
        v_mix_embedding = tf.get_variable("v_mix_embedding",
                                          shape=[
                                            self.action_size * self.goal_embedding_size + self.goal_embedding_size,
                                            1],
                                          initializer=normalized_columns_initializer(1.0))
        v_mix = tf.matmul(tf.concat([value_features,
                                     self.g_sum], 1), v_mix_embedding,
                                   name="fc_option_value")
        self.v_mix = tf.squeeze(v_mix, 1)

      with tf.variable_scope("option_worker_pi"):
        policy = tf.einsum('bj,bij->bi', self.g_sum, policy_features)
        self.g_policy = tf.nn.softmax(policy, name="policy")

        self.summaries_option.append(tf.contrib.layers.summarize_activation(self.g_policy))

      self.state_in = [self.worker_lstm.state_in[0],
                       self.worker_lstm.state_in[1],
                       self.manager_lstm.state_in[0],
                       self.manager_lstm.state_in[1]
                       ]
      self.state_out = [self.worker_lstm.state_out[0],
                        self.worker_lstm.state_out[1],
                        self.manager_lstm.state_out[0],
                        self.manager_lstm.state_out[1]
                        ]

      if self.scope != 'global':
        self.build_losses()
        self.gradients_and_summaries()

  def build_losses(self):
    """Get the probabilities for each action taken under the intra-option policy"""
    self.responsible_actions = self.get_responsible_actions(self.g_policy, self.actions_placeholder)

    """Building losses"""
    with tf.name_scope('sf_loss'):
      sf_td_error = self.target_sf - self.sf
      self.sf_loss = tf.reduce_mean(self.config.sf_coef * huber_loss(sf_td_error))

    with tf.name_scope('mix_critic_loss'):
      mix_td_error = self.target_mix_return - self.v_mix
      self.mix_critic_loss = tf.reduce_mean(0.5 * tf.square(mix_td_error))

    with tf.name_scope('goal_critic_loss'):
      td_error = self.target_return - self.v_ext
      self.critic_loss = tf.reduce_mean(0.5 * tf.square(td_error))

    with tf.name_scope('goal_loss'):
      self.goal_loss = -tf.reduce_mean(
        self.cosine_similarity(self.target_goal, self.g, 1) * tf.stop_gradient(td_error))

    with tf.name_scope('entropy_loss'):
      self.entropy_loss = -self.entropy_coef * tf.reduce_mean(self.g_policy * tf.log(self.g_policy + 1e-7))

    with tf.name_scope('policy_loss'):
      self.policy_loss = -tf.reduce_mean(tf.log(self.responsible_actions + 1e-7) * tf.stop_gradient(mix_td_error))

    self.option_loss = self.policy_loss - self.entropy_loss + self.mix_critic_loss

  def l2_normalize(self, x, axis):
      norm = tf.sqrt(tf.reduce_sum(tf.square(x), axis=axis, keepdims=True))
      return tf.maximum(x, 1e-8) / tf.maximum(norm, 1e-8)

  def cosine_similarity(self, v1, v2, axis):
    v1_norm = self.l2_normalize(v1, axis)
    v2_norm = self.l2_normalize(v2, axis)
    sim = tf.matmul(
      v1_norm, v2_norm, transpose_b=True)

    return sim

  """Build gradients for the losses with respect to the network params.
      Build summaries and update ops"""
  def gradients_and_summaries(self):
    local_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, self.scope)

    """Gradients and update ops"""
    self.grads_sf, self.apply_grads_sf = self.take_gradient(self.sf_loss)
    self.grads_option, self.apply_grads_option = self.take_gradient(self.option_loss)
    self.grads_critic, self.apply_grads_critic = self.take_gradient(self.critic_loss)
    self.grads_goal, self.apply_grads_goal = self.take_gradient(self.goal_loss)

    """Summaries"""
    self.merged_summary_sf = tf.summary.merge(self.image_summaries +
      self.summaries_sf + [tf.summary.scalar('SF_loss', self.sf_loss),
        gradient_summaries(zip(self.grads_sf, local_vars))])

    self.merged_summary_option = tf.summary.merge(self.summaries_option +\
                       [tf.summary.scalar('Entropy_loss', self.entropy_loss),
                        tf.summary.scalar('Policy_loss', self.policy_loss),
                        tf.summary.scalar('Mix_critic_loss', self.mix_critic_loss), ])
    self.merged_summary_critic = tf.summary.merge(self.summaries_critic +\
                                                  [tf.summary.scalar('Critic_loss', self.critic_loss)])
    self.merged_summary_goal = tf.summary.merge(
                                                [tf.summary.scalar('goal_loss', self.goal_loss)])

  def init_clustering(self):
    if self.scope == 'global':
      l = "0"
      if self.config.resume:
        checkpoint = self.config.load_from
        ckpt = tf.train.get_checkpoint_state(os.path.join(checkpoint, "models"))
        model_checkpoint_path = ckpt.model_checkpoint_path
        episode_checkpoint = os.path.basename(model_checkpoint_path).split(".")[0].split("-")[1]
        l = episode_checkpoint

      cluster_model_path = os.path.join(self.config.logdir, "cluster_models")
      self.goal_clusters_path = os.path.join(cluster_model_path, "goal_clusters_{}.pkl".format(l))

      """If the path exists, load them. Otherwise initialize all goals with zeros"""
      if os.path.exists(self.goal_clusters_path):
        self.goal_clusters = np.load(self.goal_clusters_path)
        self.goals_init = True
      else:
        self.goal_clusters = OnlineCluster(self.config.max_clusters, self.config.nb_options, self.goal_embedding_size)#np.zeros((self.config.nb_options, self.config.sf_layers[-1]))
        self.goals_init = False