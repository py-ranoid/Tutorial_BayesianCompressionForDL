# libraries
from __future__ import print_function
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
from torch.autograd import Variable

import BayesianLayers
from compression import compute_compression_rate, compute_reduced_weights
# from utils import visualize_pixel_importance, generate_gif, visualise_weights

N = 60000.  # number of data points in the training set


def main():
    # import data
    kwargs = {'num_workers': 1, 'pin_memory': True} if FLAGS.cuda else {}

    dataset_path = './' + FLAGS.dataset + '_data'
    ds = datasets.MNIST if FLAGS.dataset == 'mnist' else datasets.CIFAR10
    # print (dataset_path)
    train_loader = torch.utils.data.DataLoader(
        ds(dataset_path, train=True, download=True,
           transform=transforms.Compose([
               transforms.ToTensor(), lambda x: 2 * (x - 0.5),
           ])),
        batch_size=FLAGS.batchsize, shuffle=True, **kwargs)

    test_loader = torch.utils.data.DataLoader(
        ds(dataset_path, train=False, transform=transforms.Compose([
            transforms.ToTensor(), lambda x: 2 * (x - 0.5),
        ])),
        batch_size=FLAGS.batchsize, shuffle=True, **kwargs)

    # for later analysis we take some sample digits
    # mask = 255. * (np.ones((1, 28, 28)))
    # examples = train_loader.sampler.data_source.train_data[0:5].numpy()
    # images = np.vstack([mask, examples])

    # build a simple MLP
    class Net(nn.Module):
        def __init__(self):
            super(Net, self).__init__()
            # activation
            self.relu = nn.ReLU()
            # layers
            input_channels = 1 if FLAGS.dataset == 'mnist' else 3
            self.conv1 = BayesianLayers.Conv2dGroupNJ(
                input_channels, 6, 5, cuda=FLAGS.cuda)
            self.conv2 = BayesianLayers.Conv2dGroupNJ(
                6, 16, 5, cuda=FLAGS.cuda)
            num_units_fc1 = 256 if FLAGS.dataset == 'mnist' else 400
            self.fc1 = BayesianLayers.LinearGroupNJ(
                num_units_fc1, 120, clip_var=0.04, cuda=FLAGS.cuda)
            self.fc2 = BayesianLayers.LinearGroupNJ(120, 84, cuda=FLAGS.cuda)
            self.fc3 = BayesianLayers.LinearGroupNJ(84, 10, cuda=FLAGS.cuda)
            # layers including kl_divergence
            self.kl_list = [self.conv1, self.conv2,
                            self.fc1, self.fc2, self.fc3]

        def forward(self, x):
            out = F.relu(self.conv1(x))
            out = F.max_pool2d(out, 2)
            out = F.relu(self.conv2(out))
            out = F.max_pool2d(out, 2)
            out = out.view(out.size(0), -1)
            out = F.relu(self.fc1(out))
            out = F.relu(self.fc2(out))
            out = self.fc3(out)
            return out

        def get_masks(self, thresholds):
            weight_masks = []
            mask = None
            for i, (layer, threshold) in enumerate(zip(self.kl_list, thresholds)):
                # compute dropout mask
                if layer.get_type() == 'linear':
                    if mask is None:
                        log_alpha = layer.get_log_dropout_rates().cpu().data.numpy()
                        mask = log_alpha < threshold
                    else:
                        mask = np.copy(next_mask)
                    try:
                        log_alpha = layers[i +
                                           1].get_log_dropout_rates().cpu().data.numpy()
                        next_mask = log_alpha < thresholds[i + 1]
                    except:
                        # must be the last mask
                        next_mask = np.ones(10)

                    weight_mask = np.expand_dims(
                        mask, axis=0) * np.expand_dims(next_mask, axis=1)
                else:
                    in_ch = layer.in_channels
                    out_ch = layer.out_channels
                    ks = layer.kernel_size[0]

                    log_alpha = layer.get_log_dropout_rates()
                    msk = (log_alpha < threshold).type(torch.FloatTensor)

                    temp = torch.ones(out_ch, in_ch, ks, ks)

                    for k in range(len(msk)):
                        temp[k] = msk[k].expand(in_ch, ks, ks) * temp[k]

                    weight_mask = temp.cpu().data.numpy()

                weight_masks.append(weight_mask.astype(np.float))
            return weight_masks

        def kl_divergence(self):
            KLD = 0
            for layer in self.kl_list:
                KLD += layer.kl_divergence()
            return KLD

    # init model
    model = Net()
    if FLAGS.cuda:
        model.cuda()

    # init optimizer
    optimizer = optim.Adam(model.parameters())

    # we optimize the variational lower bound scaled by the number of data
    # points (so we can keep our intuitions about hyper-params such as the learning rate)
    discrimination_loss = nn.functional.cross_entropy

    def objective(output, target, kl_divergence):
        discrimination_error = discrimination_loss(output, target)
        variational_bound = discrimination_error + kl_divergence / N
        if FLAGS.cuda:
            variational_bound = variational_bound.cuda()
        return variational_bound

    def train(epoch):
        model.train()
        for batch_idx, (data, target) in enumerate(train_loader):
            if FLAGS.cuda:
                data, target = data.cuda(), target.cuda()
            data, target = Variable(data), Variable(target)
            optimizer.zero_grad()
            output = model(data)
            loss = objective(output, target, model.kl_divergence())
            loss.backward()
            optimizer.step()
            # clip the variances after each step
            for layer in model.kl_list:
                layer.clip_variances()
        print('Epoch: {} \tTrain loss:  {}\t'.format(epoch, loss.item()))

    def test():
        model.eval()
        with torch.no_grad():
            test_loss = 0
            correct = 0
            for data, target in test_loader:
                if FLAGS.cuda:
                    data, target = data.cuda(), target.cuda()
                data, target = Variable(data), Variable(target)
                output = model(data)
                test_loss += discrimination_loss(output,
                                                 target, size_average=False).item()
                pred = output.data.max(1, keepdim=True)[1]
                correct += pred.eq(target.data.view_as(pred)).cpu().sum()
            test_loss /= len(test_loader.dataset)
            print('Test loss: {:.4f}, Accuracy: {}/{} ({:.2f}%)\n'.format(
                test_loss, correct, len(test_loader.dataset),
                100. * correct / len(test_loader.dataset)))

    # train the model and save some visualisations on the way
    for epoch in range(1, FLAGS.epochs + 1):
        train(epoch)
        test()
        # visualizations
        weight_mus = [model.conv1.weight_mu, model.conv2.weight_mu,
                      model.fc1.weight_mu, model.fc2.weight_mu]
        log_alphas = [model.conv1.get_log_dropout_rates(), model.conv2.get_log_dropout_rates(),
                      model.fc1.get_log_dropout_rates(), model.fc2.get_log_dropout_rates(),
                      model.fc3.get_log_dropout_rates()]
        # visualise_weights(weight_mus, log_alphas, epoch=epoch)
        # log_alpha = model.fc1.get_log_dropout_rates().cpu().data.numpy()
        # visualize_pixel_importance(images, log_alpha=log_alpha, epoch=str(epoch))

    # generate_gif(save='pixel', epochs=FLAGS.epochs)
    # generate_gif(save='weight2_e', epochs=FLAGS.epochs)
    # generate_gif(save='weight3_e', epochs=FLAGS.epochs)

    # compute compression rate and new model accuracy
    layers = [model.conv1, model.conv2, model.fc1, model.fc2, model.fc3]
    # thresholds = FLAGS.thresholds
    threshold_vals = [[-0.6, -0.45, -2.8, -3., -5.],
                      ]
    for i, thresholds in enumerate(threshold_vals):
        compute_compression_rate(layers, model.get_masks(thresholds))

        print(i, thresholds, "Test error after with reduced bit precision:")

        weights = compute_reduced_weights(layers, model.get_masks(thresholds))
        for layer, weight in zip(layers, weights):
            if FLAGS.cuda:
                layer.post_weight_mu.data = torch.Tensor(weight).cuda()
            else:
                layer.post_weight_mu.data = torch.Tensor(weight)
        for layer in layers:
            layer.deterministic = True
        test()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=30)
    parser.add_argument('--batchsize', type=int, default=128)
    parser.add_argument('--dataset', type=str, default='cifar10')
    parser.add_argument('--thresholds', type=float,
                        nargs='*', default=[-1, -1, -2.8, -3., -5.])

    FLAGS = parser.parse_args()
    # check if we can put the net on the GPU
    FLAGS.cuda = torch.cuda.is_available()
    print (FLAGS.cuda)

    main()
