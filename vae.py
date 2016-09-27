#!/usr/bin/env python3

import theano
from theano import tensor as T
import pandas as pd
from tqdm import trange,tqdm
import numpy as np
from sklearn.preprocessing import normalize
import sklearn.svm
import time
import mnist # pip3 install python-mnist
import os
import sys
import lasagne

theano.config.exception_verbosity="high"
theano.config.optimizer='None'

#theano.config.optimizer='fast_run'
#theano.config.openmp=False
#theano.config.openmp_elemwise_minsize=10
#theano.config.device='gpu'
#theano.config.floatX='float32'
lr=0.02
n_epochs = 10000
data_amplify = 0.5
data_offset = 0.25
x_sigma = 1
z_dim = None
hid_dims = None
activation_function = None
minibatch_size = None
repeat_training=1
g=None # activation function

possible_activations = {
    'sigmoid': T.nnet.sigmoid,

    # 2.37 seems to make a sigmoid a good approximation for erf(x),
    'pseudogelu': lambda x: x * T.nnet.sigmoid(x*2.37),

    'gelu': lambda x : x*T.erf(x),
    'elu': T.nnet.elu,
    'relu': T.nnet.relu
}

class Logger():
    def __init__(self,basename=""):
        self.filename = basename+"_"+str(time.time())+".log"
        self.f = open(self.filename,'w')

    def __call__(self, *args):
        print(*args, flush=True)
        print(*args,file=self.f, flush=True)

log = None

def make_net(input_var,in_dim,hid_dim,out_dim,name=""):
    input_var_reshaped = input_var.reshape((1, in_dim))
    l_in = lasagne.layers.InputLayer((1,in_dim),input_var=input_var_reshaped,name=name+"_in")
    l_hid = lasagne.layers.DenseLayer(l_in,hid_dim,nonlinearity=g,name=name+"_hid")
    l_out = lasagne.layers.DenseLayer(l_hid,out_dim,nonlinearity=g,name=name+"_out")
    net_output = lasagne.layers.get_output(l_out)
    net_params = lasagne.layers.get_all_params([l_in,l_hid,l_out])
    return net_output, net_params

def make_vae(x_dim,z_dim,hid_dim):
    print("make_vae with x_dim={},z_dim={},hid_dim={}".format(x_dim,z_dim,hid_dim))
    x_orig = T.fmatrix('x_orig')
    z_dist,recog_params = make_net(x_orig,x_dim,hid_dim,z_dim*2,name="recog")
    z_dist.name="z_dist"
    epsilon = T.shared_randomstreams.RandomStreams().normal((z_dim,),avg=0.0,std=1.0)
    epsilon.name = 'epsilon'
    z_mu = z_dist[:,0:z_dim]
    z_mu.name = 'z_mu'
    z_sigma = z_dist[:,z_dim:z_dim*2]
    z_sigma.name = 'z_sigma'
    z_sample = z_mu + (epsilon * z_sigma)
    z_sample.name = 'z_sample'
    z_sample_reshaped = z_sample.reshape((z_dim,))
    x_out,gener_params = make_net(z_sample_reshaped,z_dim,hid_dim,x_dim,name="gener")
    params = recog_params + gener_params
    return params,x_orig,x_out,z_mu,z_sigma,z_sample

def shuffle(X,Y):
    sel = np.arange(X.shape[1])
    np.random.shuffle(sel)
    X = X[:,sel]
    Y = Y[:,sel]
    return X,Y

def fix_data(features,labels):
    # please notice the transpose '.T' operator
    # in a neural network, the datapoints needs to be scattered across the columns
    # because dot product.
    X = (np.array(features).T.astype('float32')/255.)*data_amplify + data_offset
    Y = np.expand_dims(np.array(labels).astype('float32'),1).T
    return X,Y

def load_data():
    print("setting up mnist loader..")
    _mnist = mnist.MNIST(path='./python-mnist/data')
    print("loading training data..")
    X_train,Y_train = fix_data(*_mnist.load_training())
    print("X_train.shape=",X_train.shape,"Y_train.shape=",Y_train.shape)
    print("loading testing data..")
    X_test,Y_test = fix_data(*_mnist.load_testing())
    print("X_test.shape=",X_test.shape,"Y_test.shape=",Y_test.shape)
    return X_train, Y_train, X_test, Y_test

def update(learnable, grad):
    learnable -= lr * grad

def step(xs, params, params_update_fn):
    for i in range(xs.shape[1]):
        x = xs[:,[i]].T
        params_update_fn(x)

def partition(a):
    assert type(a) is np.ndarray
    assert a.shape[1] > minibatch_size, "a.shape[1] should be larger than the minibatch size. a.shape=%s"%str(a.shape)
    minibatches_num = int(a.shape[1] / minibatch_size)
    assert minibatches_num > 0
    off = lambda i : i * minibatch_size
    return [
        a[:,off(i):off(i+1)]
        for i
        in range(minibatches_num)
    ]

def train(X, params, params_update_fn, repeat=1):
    for xs in tqdm(partition(X)*repeat,desc="training"):
        step(xs, params, params_update_fn)

def nll_sum(Z, X, Ws_vals, biases_vals, nll_fn):
    ret = 0
    for zs,xs in tqdm(partition_minibatches(Z,X),desc="nll_sum"):
        curr, = nll_fn(*([zs, xs] + Ws_vals + biases_vals))
        ret += curr
    return ret

def build_obj(z_sample,z_mu,z_sigma,x_orig,x_out):
    log_q_z_given_x = - 0.5*T.dot((1/z_sigma), ((z_sample-z_mu)**2).T) # plus log(C) that can be omitted
    det_z_sigma = T.prod(z_sigma)
    C = ((2*3.1415)**(z_dim/2)) * (det_z_sigma**2)
    q_z_given_x = C * T.exp(log_q_z_given_x)
    log_p_x_given_z = -(1/x_sigma)*(((x_orig-x_out)**2).sum()) # because p(x|z) is gaussian
    log_p_z = - (z_sample**2).sum() # gaussian prior with mean 0 and cov I
    reconstruction_error = -(q_z_given_x * log_p_x_given_z)
    regularizer = -(q_z_given_x * log_p_z) + log_q_z_given_x
    obj = reconstruction_error + regularizer
    obj_scalar = obj.reshape((),ndim=0)
    return obj_scalar

def test_classifier(Z,Y):
    #classifier = sklearn.svm.SVC()
    log("training classifier..")
    classifier = sklearn.svm.SVC(
        kernel='rbf',
        max_iter=1000
    )
    # please notice the transpose '.T' operator: sklearn wants one datapoint per row
    classifier.fit(Z.T,Y[0,:])
    log("done. Scoring..")
    svc_score = classifier.score(Z.T,Y[0,:])
    log("SVC score: %s"%svc_score)

def generate_samples(epoch,Ws_vals,biases_vals,generate_fn):
    log("generating a bunch of random samples")
    _zs_l = []
    for i in range(minibatch_size):
        _z = np.random.normal(np.array([0]*z_dim),sigma_z).astype('float32')
        _zs_l.append(_z)
    _zs = np.vstack(_zs_l).T
    samples = generate_fn(*([_zs]+Ws_vals+biases_vals))
    log("generated samples. mean:",np.mean(samples),"std:",np.std(samples))
    log("_zs",_zs)
    filename = "random_samples_epoch_%d.npy"%(epoch)
    np.save(filename, samples)
    log("done generating random samples.")

def main():
    global log
    global z_dim
    global hid_dims
    global minibatch_size
    global activation_function
    global g
    assert len(sys.argv) > 1, "usage: %s harvest_dir"%(sys.argv[0])
    z_dim = int(sys.argv[1])
    hid_dim = int(sys.argv[2])
    minibatch_size = int(sys.argv[3])
    activation_name = sys.argv[4]
    g = possible_activations[activation_name]

    harvest_dir = "harvest_zdim{}_hdim_{}_minibatch_size_{}_activation_{}".format(
        z_dim,
        hid_dim,
        minibatch_size,
        activation_name
    )
    np.set_printoptions(precision=4, suppress=True)
    X,Y,X_test,Y_test = load_data() # needs to be before cd
    try:
        os.mkdir(harvest_dir)
    except OSError as e: # directory already exists. It's ok.
        print(e)

    os.system("cp %s %s -vf"%(sys.argv[0],harvest_dir+"/"))
    os.chdir(harvest_dir)
    log = Logger()
    log("sys.argv",sys.argv)
    x_dim = X.shape[0]
    num_datapoints = X.shape[1]
    # set up
    params,x_orig,x_out,z_mu,z_sigma,z_sample = make_vae(x_dim,z_dim,hid_dim)
    obj = build_obj(z_mu,z_sigma,z_sample,x_orig,x_out)
    #minibatch_obj = T.sum(objs,axis=0)

    grads_params = [
        T.grad(obj,curr)
        for curr
        in params
    ]
    params_updates = lasagne.updates.adam(grads_params,params,learning_rate=lr)
    params_update_fn = theano.function([x_orig],[], updates=params_updates)

    def summary():
        #total_nll = nll_sum(Z,X,Ws_vals,biases_vals,nll_fn)
        log("epoch %d"%epoch)
        log("harvest_dir",harvest_dir)
        log("lr %f"%lr)
        #log("total nll: {:,}".format(total_nll))

    log("done. epochs loop..")

    def save():
        log("saving Y,Ws,biases..")
        np.save("theano_decoder_Z.npy",Z)
        np.save("theano_decoder_Y.npy",Y)
        for i, (_w,_b) in enumerate(zip(Ws_vals,biases_vals)):
            np.save('theano_decoder_W_{}.npy'.format(i), _w)
            np.save('theano_decoder_bias_{}.npy'.format(i), _b)
        log("done saving.")

    # train
    for epoch in range(n_epochs):
        X,Y = shuffle(X,Y)
        summary()
        if epoch % 5 == 0:
            print("(TODO)")
            #generate_samples(epoch,Ws_vals,biases_vals,generate_fn)
            #save()
        train(X,params,params_update_fn,repeat=repeat_training)
    log("epochs loop ended")
    summary()
if __name__=="__main__":
    main()
