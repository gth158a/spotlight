"""
Factorization models for explicit feedback problems.
"""

import numpy as np

import torch

import torch.optim as optim

from spotlight.helpers import _repr_model
from spotlight.factorization._components import (_predict_process_features,
                                                 _predict_process_ids)
from spotlight.factorization.representations import (BilinearNet,
                                                     FeatureNet,
                                                     HybridContainer)
from spotlight.losses import (regression_loss,
                              poisson_loss)
from spotlight.torch_utils import cpu, gpu, set_seed


class ExplicitFactorizationModel(object):
    """
    An explicit feedback matrix factorization model. Uses a classic
    matrix factorization [1]_ approach, with latent vectors used
    to represent both users and items. Their dot product gives the
    predicted score for a user-item pair.

    The latent representation is given by
    :class:`spotlight.factorization.representations.BilinearNet`.

    .. [1] Koren, Yehuda, Robert Bell, and Chris Volinsky.
       "Matrix factorization techniques for recommender systems."
       Computer 42.8 (2009).

    Parameters
    ----------

    loss: string, optional
        One of 'regression', 'poisson',
        corresponding to losses from :class:`spotlight.losses`.
    embedding_dim: int, optional
        Number of embedding dimensions to use for users and items.
    n_iter: int, optional
        Number of iterations to run.
    batch_size: int, optional
        Minibatch size.
    l2: float, optional
        L2 loss penalty.
    learning_rate: float, optional
        Initial learning rate.
    optimizer: instance of a PyTorch optimizer, optional
        Overrides l2 and learning rate if supplied.
    use_cuda: boolean, optional
        Run the model on a GPU.
    sparse: boolean, optional
        Use sparse gradients for embedding layers.
    random_state: instance of numpy.random.RandomState, optional
        Random state to use when fitting.
    """

    def __init__(self,
                 loss='regression',
                 embedding_dim=32,
                 n_iter=10,
                 batch_size=256,
                 l2=0.0,
                 learning_rate=1e-2,
                 optimizer=None,
                 use_cuda=False,
                 sparse=False,
                 random_state=None):

        assert loss in ('regression',
                        'poisson')

        self._loss = loss
        self._embedding_dim = embedding_dim
        self._n_iter = n_iter
        self._learning_rate = learning_rate
        self._batch_size = batch_size
        self._l2 = l2
        self._use_cuda = use_cuda
        self._sparse = sparse
        self._optimizer = None
        self._random_state = random_state or np.random.RandomState()

        self._num_users = None
        self._num_items = None
        self._net = None

        set_seed(self._random_state.randint(-10**8, 10**8),
                 cuda=self._use_cuda)

    def __repr__(self):

        return _repr_model(self)

    def fit(self, interactions, verbose=False):
        """
        Fit the model.

        Parameters
        ----------

        interactions: :class:`spotlight.interactions.Interactions`
            The input dataset. Must have ratings.
        """

        (self._num_users,
         self._num_items) = (interactions.num_users,
                             interactions.num_items)

        latent_net = BilinearNet(self._num_users,
                                 self._num_items,
                                 self._embedding_dim,
                                 sparse=self._sparse)

        if interactions.num_user_features():
            user_net = FeatureNet(interactions.num_user_features(),
                                  self._embedding_dim)
        else:
            user_net = None

        if interactions.num_context_features():
            context_net = FeatureNet(interactions.num_context_features(),
                                     self._embedding_dim)
        else:
            context_net = None

        if interactions.num_item_features():
            item_net = FeatureNet(interactions.num_item_features(),
                                  self._embedding_dim)
        else:
            item_net = None

        self._net = gpu(HybridContainer(latent_net,
                                        user_net,
                                        context_net,
                                        item_net),
                        self._use_cuda)

        if self._optimizer is None:
            self._optimizer = optim.Adam(
                self._net.parameters(),
                weight_decay=self._l2,
                lr=self._learning_rate
            )

        if self._loss == 'regression':
            loss_fnc = regression_loss
        elif self._loss == 'poisson':
            loss_fnc = poisson_loss
        else:
            raise ValueError('Unknown loss: {}'.format(self._loss))

        for epoch_num in range(self._n_iter):

            interactions.shuffle(random_state=self._random_state)

            epoch_loss = 0.0

            for (minibatch_num,
                 minibatch) in enumerate(interactions
                                         .minibatches(batch_size=self._batch_size)):

                minibatch = minibatch.torch(self._use_cuda).variable()

                predictions = self._net(minibatch.user_ids,
                                        minibatch.item_ids,
                                        minibatch.user_features,
                                        minibatch.context_features,
                                        minibatch.get_item_features(
                                            minibatch.item_ids
                                        ))

                if self._loss == 'poisson':
                    predictions = torch.exp(predictions)

                self._optimizer.zero_grad()

                loss = loss_fnc(minibatch.ratings, predictions)
                epoch_loss += loss.data[0]

                loss.backward()
                self._optimizer.step()

            epoch_loss /= minibatch_num + 1

            if verbose:
                print('Epoch {}: loss {}'.format(epoch_num, epoch_loss))

    def predict(self, user_ids, item_ids=None,
                user_features=None,
                context_features=None,
                item_features=None):
        """
        Make predictions: given a user id, compute the recommendation
        scores for items.

        Parameters
        ----------

        user_ids: int or array
           If int, will predict the recommendation scores for this
           user for all items in item_ids. If an array, will predict
           scores for all (user, item) pairs defined by user_ids and
           item_ids.
        item_ids: array, optional
            Array containing the item ids for which prediction scores
            are desired. If not supplied, predictions for all items
            will be computed.

        Returns
        -------

        predictions: np.array
            Predicted scores for all items in item_ids.
        """

        self._net.train(False)

        user_ids, item_ids = _predict_process_ids(user_ids, item_ids,
                                                  self._num_items,
                                                  self._use_cuda)

        (user_features,
         context_features,
         item_features) = _predict_process_features(user_features,
                                                    context_features,
                                                    item_features,
                                                    len(item_ids),
                                                    self._use_cuda)

        out = self._net(user_ids,
                        item_ids,
                        user_features,
                        context_features,
                        item_features)

        if self._loss == 'poisson':
            out = torch.exp(out)

        return cpu(out.data).numpy().flatten()
