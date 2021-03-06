#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numpy as np
import os
import paddle.v2 as paddle
import paddle.fluid as fluid


def stacked_lstm_net(data,
                     label,
                     input_dim,
                     class_dim=2,
                     emb_dim=128,
                     hid_dim=512,
                     stacked_num=3):
    assert stacked_num % 2 == 1

    emb = fluid.layers.embedding(input=data, size=[input_dim, emb_dim])
    # add bias attr

    # TODO(qijun) linear act
    fc1 = fluid.layers.fc(input=emb, size=hid_dim)
    lstm1, cell1 = fluid.layers.dynamic_lstm(input=fc1, size=hid_dim)

    inputs = [fc1, lstm1]

    for i in range(2, stacked_num + 1):
        fc = fluid.layers.fc(input=inputs, size=hid_dim)
        lstm, cell = fluid.layers.dynamic_lstm(
            input=fc, size=hid_dim, is_reverse=(i % 2) == 0)
        inputs = [fc, lstm]

    fc_last = fluid.layers.sequence_pool(input=inputs[0], pool_type='max')
    lstm_last = fluid.layers.sequence_pool(input=inputs[1], pool_type='max')

    prediction = fluid.layers.fc(input=[fc_last, lstm_last],
                                 size=class_dim,
                                 act='softmax')
    cost = fluid.layers.cross_entropy(input=prediction, label=label)
    avg_cost = fluid.layers.mean(x=cost)
    adam_optimizer = fluid.optimizer.Adam(learning_rate=0.002)
    optimize_ops, params_grads = adam_optimizer.minimize(avg_cost)
    accuracy = fluid.evaluator.Accuracy(input=prediction, label=label)
    return avg_cost, accuracy, accuracy.metrics[0], optimize_ops, params_grads


def to_lodtensor(data, place):
    seq_lens = [len(seq) for seq in data]
    cur_len = 0
    lod = [cur_len]
    for l in seq_lens:
        cur_len += l
        lod.append(cur_len)
    flattened_data = np.concatenate(data, axis=0).astype("int64")
    flattened_data = flattened_data.reshape([len(flattened_data), 1])
    res = fluid.LoDTensor()
    res.set(flattened_data, place)
    res.set_lod([lod])
    return res


def main():
    BATCH_SIZE = 100
    PASS_NUM = 5

    word_dict = paddle.dataset.imdb.word_dict()
    print "loaded word dict successfully"
    dict_dim = len(word_dict)
    class_dim = 2

    data = fluid.layers.data(
        name="words", shape=[1], dtype="int64", lod_level=1)
    label = fluid.layers.data(name="label", shape=[1], dtype="int64")
    cost, accuracy, acc_out, optimize_ops, params_grads = stacked_lstm_net(
        data, label, input_dim=dict_dim, class_dim=class_dim)

    train_data = paddle.batch(
        paddle.reader.shuffle(
            paddle.dataset.imdb.train(word_dict), buf_size=1000),
        batch_size=BATCH_SIZE)
    place = fluid.CPUPlace()
    exe = fluid.Executor(place)
    feeder = fluid.DataFeeder(feed_list=[data, label], place=place)

    t = fluid.DistributeTranspiler()
    # all parameter server endpoints list for spliting parameters
    pserver_endpoints = os.getenv("PSERVERS")
    # server endpoint for current node
    current_endpoint = os.getenv("SERVER_ENDPOINT")
    # run as trainer or parameter server
    training_role = os.getenv(
        "TRAINING_ROLE", "TRAINER")  # get the training role: trainer/pserver
    t.transpile(
        optimize_ops, params_grads, pservers=pserver_endpoints, trainers=2)

    if training_role == "PSERVER":
        if not current_endpoint:
            print("need env SERVER_ENDPOINT")
            exit(1)
        pserver_prog = t.get_pserver_program(current_endpoint)
        pserver_startup = t.get_startup_program(current_endpoint, pserver_prog)
        exe.run(pserver_startup)
        exe.run(pserver_prog)
    elif training_role == "TRAINER":
        exe.run(fluid.default_startup_program())
        trainer_prog = t.get_trainer_program()
        for pass_id in xrange(PASS_NUM):
            accuracy.reset(exe)
            for data in train_data():
                cost_val, acc_val = exe.run(trainer_prog,
                                            feed=feeder.feed(data),
                                            fetch_list=[cost, acc_out])
                pass_acc = accuracy.eval(exe)
                print("cost=" + str(cost_val) + " acc=" + str(acc_val) +
                      " pass_acc=" + str(pass_acc))
                if cost_val < 1.0 and acc_val > 0.8:
                    exit(0)
    else:
        print("environment var TRAINER_ROLE should be TRAINER os PSERVER")


if __name__ == '__main__':
    main()
