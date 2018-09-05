from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import operator
from functools import reduce

import numpy as np
import sonnet as snt
import tensorflow as tf
import scipy

import sounds_deep.contrib.data.data as data
import sounds_deep.contrib.util.scaling as scaling
import sounds_deep.contrib.util.util as util
import sounds_deep.contrib.models.vae as vae
import sounds_deep.contrib.models.nvvae as nvvae
import sounds_deep.contrib.parameterized_distributions.discretized_logistic as discretized_logistic
import sounds_deep.contrib.util.plot as plot

parser = argparse.ArgumentParser(description='Train a VAE model.')
parser.add_argument('--batch_size', type=int, default=32)
parser.add_argument('--labeled_batch_size', type=int, default=32)
parser.add_argument('--latent_dimension', type=int, default=32)
parser.add_argument('--epochs', type=int, default=500)
parser.add_argument('--learning_rate', type=float, default=3e-5)
parser.add_argument('--dataset', type=str, default='mnist')
args = parser.parse_args()


def apply_temp(a, temperature=1.0):
    # helper function to sample an index from a probability array
    a = tf.log(a) / temperature
    a = tf.exp(a) / tf.reduce_sum(tf.exp(a), axis=1, keepdims=True)
    return a

def unison_shuffled_copies(arrays):
    assert all([len(a) == len(arrays[0]) for a in arrays])
    p = np.random.permutation(len(arrays[0]))
    return [a[p] for a in arrays]


# load the data
if args.dataset == 'cifar10':
    train_data, train_labels, _, _ = data.load_cifar10('./data/')
elif args.dataset == 'mnist':
    train_data, train_labels, test_data, test_labels = data.load_mnist(
        './data/')
    train_data = np.reshape(train_data, [-1, 28, 28, 1])
data_shape = (args.batch_size, ) + train_data.shape[1:]
label_shape = (args.batch_size, ) + train_labels.shape[1:]
batches_per_epoch = train_data.shape[0] // args.batch_size

train_data, train_labels = unison_shuffled_copies([train_data, train_labels])
labeled_train_data = train_data[:100]
labeled_train_labels = train_labels[:100]
labeled_train_gen = data.parallel_data_generator([labeled_train_data, labeled_train_labels],
                                         args.labeled_batch_size)
unlabeled_train_gen = data.parallel_data_generator([train_data, train_labels], args.batch_size)

# build the model
if args.dataset == 'cifar10':
    encoder_module = snt.Sequential([
        snt.Conv2D(16, 3),
        snt.Residual(snt.Conv2D(16, 3)),
        snt.Residual(snt.Conv2D(16, 3)), scaling.squeeze2d,
        snt.Conv2D(64, 3),
        snt.Residual(snt.Conv2D(64, 3)),
        snt.Residual(snt.Conv2D(64, 3)), scaling.squeeze2d,
        snt.Conv2D(64, 3),
        snt.Residual(snt.Conv2D(64, 3)),
        snt.Residual(snt.Conv2D(64, 3)), scaling.squeeze2d,
        snt.Conv2D(128, 3),
        snt.Residual(snt.Conv2D(128, 3)),
        snt.Residual(snt.Conv2D(128, 3)), scaling.squeeze2d,
        snt.Conv2D(256, 3),
        snt.Residual(snt.Conv2D(256, 3)),
        snt.Residual(snt.Conv2D(256, 3)), scaling.squeeze2d,
        tf.keras.layers.Flatten(),
        snt.Linear(100)
    ])
    decoder_module = snt.Sequential([
        lambda x: tf.reshape(x, [-1, 4, 4, 4]),
        snt.Conv2D(32, 3),
        snt.Residual(snt.Conv2D(32, 3)),
        snt.Residual(snt.Conv2D(32, 3))
    ] + [
        scaling.unsqueeze2d,
        snt.Conv2D(32, 3),
        snt.Residual(snt.Conv2D(32, 3)),
        snt.Residual(snt.Conv2D(32, 3))
    ] * 5)
    output_distribution_fn = discretized_logistic.DiscretizedLogistic
elif args.dataset == 'mnist':
    nv_encoder_module = snt.nets.ConvNet2D([32, 64, 128], [5], [2], [snt.SAME])
    encoder_module = snt.nets.ConvNet2D([32, 64, 128], [5], [2], [snt.SAME])
    decoder_module = snt.Sequential([
        snt.Linear(64), lambda x: tf.reshape(x, [-1, 2, 2, 16]),
        snt.nets.ConvNet2DTranspose([128, 64, 32, 32], [(4, 4), (8, 8),
                                                        (16, 16), (32, 32)],
                                    [5], [2], [snt.SAME]),
        lambda x: tf.reshape(x, [-1, 32 * 32 * 32]),
        snt.Linear(28 * 28), lambda x: tf.reshape(x, [-1, 28, 28, 1])
    ])
    output_distribution_fn = vae.BERNOULLI_FN

model = nvvae.NamedLatentVAE(
    args.latent_dimension,
    10,
    nv_encoder_module,
    encoder_module,
    decoder_module,
    output_dist_fn=output_distribution_fn)

# build model
temperature_ph = tf.placeholder(tf.float32)
labeled_data_ph = tf.placeholder(
    tf.float32, shape=(args.labeled_batch_size, ) + data_shape[1:])
unlabeled_data_ph = tf.placeholder(
    tf.float32, shape=(args.batch_size, ) + data_shape[1:])
label_ph = tf.placeholder(
    tf.float32, shape=(args.labeled_batch_size, ) + label_shape[1:])
# used exclusively to calculate classification rate
unlabeled_label_ph = tf.placeholder(
    tf.float32, shape=(args.labeled_batch_size, ) + label_shape[1:])
model(
    unlabeled_data_ph,
    labeled_data_ph,
    label_ph,
    temperature_ph,
    analytic_kl=True)

num_samples = 16
nv_sample_ph = tf.placeholder_with_default(
    tf.ones([num_samples, 10]), [num_samples, 10])
sample = model.sample(
    sample_shape=[num_samples],
    temperature=temperature_ph,
    nv_prior_sample=nv_sample_ph)
classification_rate = tf.count_nonzero(tf.equal(
        tf.argmax(tf.squeeze(model.nv_latent_posterior_sample), axis=1),
        tf.argmax(unlabeled_label_ph, axis=1)), dtype=tf.float32) / args.batch_size

optimizer = tf.train.AdamOptimizer(learning_rate=args.learning_rate)
train_op = optimizer.minimize(-model.elbo)

verbose_ops_dict = dict()
verbose_ops_dict['distortion'] = model.distortion
verbose_ops_dict['rate'] = model.rate
verbose_ops_dict['nv_entropy'] = model.nv_entropy
verbose_ops_dict['nv_log_prob'] = model.nv_log_prob
verbose_ops_dict['elbo'] = model.elbo
verbose_ops_dict['prior_logp'] = model.prior_logp
verbose_ops_dict['posterior_logp'] = model.posterior_logp
verbose_ops_dict['nv_prior_logp'] = model.nv_prior_logp
verbose_ops_dict['nv_posterior_logp'] = model.nv_posterior_logp
verbose_ops_dict['classification_rate'] = classification_rate

config = tf.ConfigProto()
config.gpu_options.allow_growth = True
with tf.Session(config=config) as session:
    session.run(tf.global_variables_initializer())
    for epoch in range(args.epochs):
        temperature = 0.5
        print("Temperature: {}".format(temperature))

        def feed_dict_fn():
            feed_dict = dict()
            labeled_arrays = next(labeled_train_gen)
            unlabeled_arrays = next(unlabeled_train_gen)
            feed_dict[unlabeled_data_ph] = unlabeled_arrays[0]
            feed_dict[labeled_data_ph] = labeled_arrays[0]
            feed_dict[label_ph] = labeled_arrays[1]
            feed_dict[unlabeled_label_ph] = unlabeled_arrays[1]
            feed_dict[temperature_ph] = temperature
            return feed_dict

        print("EPOCH {}".format(epoch))
        out_dict = util.run_epoch_ops(
            session,
            train_data.shape[0] // args.batch_size,
            verbose_ops_dict=verbose_ops_dict,
            silent_ops=[train_op],
            feed_dict_fn=feed_dict_fn,
            verbose=True)

        mean_distortion = np.mean(out_dict['distortion'])
        mean_rate = np.mean(out_dict['rate'])
        mean_nv_entropy = np.mean(out_dict['nv_entropy'])
        mean_nv_log_prob = np.mean(out_dict['nv_log_prob'])
        mean_elbo = np.mean(out_dict['elbo'])
        mean_prior_logp = np.mean(out_dict['prior_logp'])
        mean_posterior_logp = np.mean(out_dict['posterior_logp'])
        mean_nv_prior_logp = np.mean(out_dict['nv_prior_logp'])
        mean_nv_posterior_logp = np.mean(out_dict['nv_posterior_logp'])
        mean_classification_rate = np.mean(out_dict['classification_rate'])

        bits_per_dim = -mean_elbo / (
            np.log(2.) * reduce(operator.mul, data_shape[-3:]))
        print(
            "bits per dim: {:7.4f}\tdistortion: {:7.4f}\trate: {:7.4f}\tnv_entropy: {:7.4f}\tnv_log_prob: {:7.4f}\tprior_logp: {:7.4f}\tposterior_logp: {:7.4f}\telbo: {:7.4f}\tclassification_rate: {:7.4f}"
            .format(bits_per_dim, mean_distortion, mean_rate, mean_nv_entropy,
                    mean_nv_log_prob, mean_prior_logp, mean_posterior_logp,
                    mean_elbo, mean_classification_rate))

        for temp in [0.01, 0.5]:
            for c in range(10):
                nv_sample_val = np.zeros([num_samples, 10], dtype=float)
                nv_sample_val[c] = 1.
                generated_img = session.run(sample, {
                    temperature_ph: temp,
                    nv_sample_ph: nv_sample_val
                })
                plot.plot('epoch{}_class{}_temp{}.png'.format(epoch, c, temp),
                          np.squeeze(generated_img), 4, 4)