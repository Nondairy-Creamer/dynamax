import jax.numpy as jnp
import jax.random as jr
import tensorflow_probability.substrates.jax.bijectors as tfb
import tensorflow_probability.substrates.jax.distributions as tfd
from jax import vmap
from jax.scipy.special import logsumexp
from dynamax.parameters import ParameterProperties
from dynamax.distributions import NormalInverseGamma
from dynamax.distributions import NormalInverseWishart
from dynamax.distributions import nig_posterior_update
from dynamax.distributions import niw_posterior_update
from dynamax.hmm.models.abstractions import HMM, HMMEmissions
from dynamax.hmm.models.initial import StandardHMMInitialState
from dynamax.hmm.models.transitions import StandardHMMTransitions
from dynamax.utils import PSDToRealBijector, pytree_sum


class GaussianMixtureHMMEmissions(HMMEmissions):
    """
    Hidden Markov Model with Gaussian mixture emissions.
    Attributes
    ----------
    weights : array, shape (num_states, num_emission_components)
        Mixture weights for each state.
    emission_means : array, shape (num_states, num_emission_components, emission_dim)
        Mean parameters for each mixture component in each state.
    emission_covariance_matrices : array
        Covariance parameters for each mixture components in each state.
    """

    def __init__(self,
                 num_states,
                 num_components,
                 emission_dim,
                 emission_weights_concentration=1.1,
                 emission_prior_mean=0.,
                 emission_prior_mean_concentration=1e-4,
                 emission_prior_extra_df=1e-4,
                 emission_prior_scale=0.1):
        self.num_states = num_states
        self.num_components = num_components
        self.emission_dim = emission_dim
        self.emission_weights_concentration = emission_weights_concentration * jnp.ones(num_components)
        self.emission_prior_mean = emission_prior_mean * jnp.ones(emission_dim)
        self.emission_prior_mean_concentration = emission_prior_mean_concentration
        self.emission_prior_df = emission_dim + emission_prior_extra_df
        self.emission_prior_scale = emission_prior_scale * jnp.eye(emission_dim)

    @property
    def emission_shape(self):
        return (self.emission_dim,)

    def initialize(self, key=jr.PRNGKey(0),
                   method="prior",
                   emission_weights=None,
                   emission_means=None,
                   emission_covariances=None,
                   emissions=None):
        if method.lower() == "kmeans":
            assert emissions is not None, "Need emissions to initialize the model with K-Means!"
            from sklearn.cluster import KMeans
            km = KMeans(self.num_states).fit(emissions.reshape(-1, self.emission_dim))
            _emission_weights = jnp.ones((self.num_states, self.num_components)) / self.num_components
            _emission_means = jnp.tile(jnp.array(km.cluster_centers_)[:, None, :], (1, self.num_components, 1))
            _emission_covs = jnp.tile(jnp.eye(self.emission_dim), (self.num_states, self.num_components, 1, 1))

        elif method.lower() == "prior":
            key1, key2, key = jr.split(key, 3)
            _emission_weights = jr.dirichlet(key1, self.emission_weights_concentration, shape=(self.num_states,))

            prior = NormalInverseWishart(self.emission_prior_mean,
                                         self.emission_prior_mean_concentration,
                                         self.emission_prior_df,
                                         self.emission_prior_scale)
            (_emission_covs, _emission_means) = prior.sample(
                seed=key2, sample_shape=(self.num_states, self.num_components))

        else:
            raise Exception("Invalid initialization method: {}".format(method))

        # Only use the values above if the user hasn't specified their own
        default = lambda x, x0: x if x is not None else x0
        params = dict(weights=default(emission_weights, _emission_weights),
                                   means=default(emission_means, _emission_means),
                                   covs=default(emission_covariances, _emission_covs))
        props = dict(weights=ParameterProperties(constrainer=tfb.SoftmaxCentered()),
                                  means=ParameterProperties(),
                                  covs=ParameterProperties(constrainer=tfb.Invert(PSDToRealBijector)))
        return params, props

    def distribution(self, params, state, covariates=None):
        return tfd.MixtureSameFamily(
            mixture_distribution=tfd.Categorical(probs=params['weights'][state]),
            components_distribution=tfd.MultivariateNormalFullCovariance(
                loc=params['means'][state], covariance_matrix=params['covs'][state]))

    def log_prior(self, params):
        lp = tfd.Dirichlet(self.emission_weights_concentration).log_prob(
            params['weights']).sum()
        lp += NormalInverseWishart(self.emission_prior_mean, self.emission_prior_mean_concentration,
                                   self.emission_prior_df, self.emission_prior_scale).log_prob(
            (params['covs'], params['means'])).sum()
        return lp

    def collect_suff_stats(self, params, posterior, emissions, covariates=None):
        def prob_fn(x):
            logprobs = vmap(lambda mus, sigmas, weights: tfd.MultivariateNormalFullCovariance(
                loc=mus, covariance_matrix=sigmas).log_prob(x) + jnp.log(weights))(
                    params['means'], params['covs'], params['weights'])
            logprobs = logprobs - logsumexp(logprobs, axis=-1, keepdims=True)
            return jnp.exp(logprobs)

        prob_denses = vmap(prob_fn)(emissions)
        expected_states = posterior.smoothed_probs
        weights = jnp.einsum("tk,tkm->tkm", expected_states, prob_denses)
        Sx = jnp.einsum("tkm,tn->kmn", weights, emissions)
        SxxT = jnp.einsum("tkm,tn,tl->kmnl", weights, emissions, emissions)
        N = weights.sum(axis=0)
        return dict(N=N, Sx=Sx, SxxT=SxxT)

    def m_step(self, params, props, batch_stats):
        assert props['weights'].trainable, "GaussianMixtureHMM.fit_em() does not support fitting a subset of parameters"
        assert props['means'].trainable, "GaussianMixtureHMM.fit_em() does not support fitting a subset of parameters"
        assert props['covs'].trainable, "GaussianMixtureHMM.fit_em() does not support fitting a subset of parameters"

        niw_prior = NormalInverseWishart(self.emission_prior_mean,
                                         self.emission_prior_mean_concentration,
                                         self.emission_prior_df,
                                         self.emission_prior_scale)

        def _single_m_step(Sx, SxxT, N):
            """Update the parameters for one discrete state"""
            # Update the component probabilities (i.e. weights)
            nu_post = self.emission_weights_concentration + N
            weights = tfd.Dirichlet(nu_post).mode()

            # Update the mean and covariance for each component
            covs, means = vmap(lambda stats: niw_posterior_update(niw_prior, stats).mode())((Sx, SxxT, N))
            return weights, means, covs

        emission_stats = pytree_sum(batch_stats, axis=0)
        weights, means, covs = vmap(_single_m_step)(
            emission_stats['Sx'], emission_stats['SxxT'], emission_stats['N'])
        params['weights'] = weights
        params['means'] = means
        params['covs'] = covs
        return params


class DiagonalGaussianMixtureHMMEmissions(HMMEmissions):
    def __init__(self,
                 num_states,
                 num_components,
                 emission_dim,
                 emission_weights_concentration=1.1,
                 emission_prior_mean=0.,
                 emission_prior_mean_concentration=1e-4,
                 emission_prior_shape=1.,
                 emission_prior_scale=1.):
        self.num_states = num_states
        self.num_components = num_components
        self.emission_dim = emission_dim

        self.emission_weights_concentration = \
            emission_weights_concentration * jnp.ones(num_components)
        self.emission_prior_mean = emission_prior_mean
        self.emission_prior_mean_concentration = emission_prior_mean_concentration
        self.emission_prior_shape = emission_prior_shape
        self.emission_prior_scale = emission_prior_scale

    @property
    def emission_shape(self):
        return (self.emission_dim,)

    def initialize(self, key=jr.PRNGKey(0),
                   method="prior",
                   emission_weights=None,
                   emission_means=None,
                   emission_scale_diags=None,
                   emissions=None):
        if method.lower() == "kmeans":
            assert emissions is not None, "Need emissions to initialize the model with K-Means!"
            from sklearn.cluster import KMeans
            km = KMeans(self.num_states).fit(emissions.reshape(-1, self.emission_dim))
            _emission_weights = jnp.ones((self.num_states, self.num_components)) / self.num_components
            _emission_means = jnp.tile(jnp.array(km.cluster_centers_)[:, None, :], (1, self.num_components, 1))
            _emission_scale_diags = jnp.ones((self.num_states, self.num_components, self.emission_dim))

        elif method.lower() == "prior":
            key1, key2, key = jr.split(key, 3)
            _emission_weights = jr.dirichlet(key1, self.emission_weights_concentration, shape=(self.num_states,))

            prior = NormalInverseGamma(self.emission_prior_mean,
                                       self.emission_prior_mean_concentration,
                                       self.emission_prior_shape,
                                       self.emission_prior_scale)
            (_emission_scale_diags, _emission_means) = prior.sample(
                seed=key2, sample_shape=(self.num_states, self.num_components, self.emission_dim))

        else:
            raise Exception("Invalid initialization method: {}".format(method))

        # Only use the values above if the user hasn't specified their own
        default = lambda x, x0: x if x is not None else x0
        params = dict(weights=default(emission_weights, _emission_weights),
                      means=default(emission_means, _emission_means),
                      scale_diags=default(emission_scale_diags, _emission_scale_diags))
        props = dict(weights=ParameterProperties(constrainer=tfb.SoftmaxCentered()),
                     means=ParameterProperties(),
                     scale_diags=ParameterProperties(constrainer=tfb.Softplus()))
        return params, props

    def distribution(self, params, state, covariates=None):
        return tfd.MixtureSameFamily(
            mixture_distribution=tfd.Categorical(probs=params['weights'][state]),
            components_distribution=tfd.MultivariateNormalDiag(
                loc=params['means'][state],
                scale_diag=params['scale_diags'][state]))

    def log_prior(self, params):
        lp = tfd.Dirichlet(self.emission_weights_concentration).log_prob(
            params['weights']).sum()
        lp += NormalInverseGamma(self.emission_prior_mean, self.emission_prior_mean_concentration,
                                   self.emission_prior_shape, self.emission_prior_scale).log_prob(
            (params['scale_diags']**2, params['means'])).sum()
        return lp

    # Expectation-maximization (EM) code
    def collect_suff_stats(self, params, posterior, emissions, covariates=None):
        # Evaluate the posterior probability of each discrete class
        def prob_fn(x):
            logprobs = vmap(lambda mus, sigmas, weights: tfd.MultivariateNormalDiag(
                loc=mus, scale_diag=sigmas).log_prob(x) + jnp.log(weights))(
                    params['means'], params['scale_diags'],
                    params['weights'])
            logprobs = logprobs - logsumexp(logprobs, axis=-1, keepdims=True)
            return jnp.exp(logprobs)

        prob_denses = vmap(prob_fn)(emissions)
        expected_states = posterior.smoothed_probs
        weights = jnp.einsum("tk,tkm->tkm", expected_states, prob_denses)
        Sx = jnp.einsum("tkm,tn->kmn", weights, emissions)
        Sxsq = jnp.einsum("tkm,tn,tn->kmn", weights, emissions, emissions)
        N = weights.sum(axis=0)
        return dict(N=N, Sx=Sx, Sxsq=Sxsq)

    def m_step(self, params, props, batch_stats):
        assert props['weights'].trainable, "GaussianMixtureDiagHMM.fit_em() does not support fitting a subset of parameters"
        assert props['means'].trainable, "GaussianMixtureDiagHMM.fit_em() does not support fitting a subset of parameters"
        assert props['scale_diags'].trainable, "GaussianMixtureDiagHMM.fit_em() does not support fitting a subset of parameters"

        nig_prior = NormalInverseGamma(
            self.emission_prior_mean, self.emission_prior_mean_concentration,
            self.emission_prior_shape, self.emission_prior_scale)

        def _single_m_step(Sx, Sxsq, N):
            """Update the parameters for one discrete state"""
            # Update the component probabilities (i.e. weights)
            nu_post = self.emission_weights_concentration + N
            mixture_weights = tfd.Dirichlet(nu_post).mode()

            # Update the mean and variances for each component
            var_diags, means = vmap(lambda stats: nig_posterior_update(nig_prior, stats).mode())((Sx, Sxsq, N))
            scale_diags = jnp.sqrt(var_diags)
            return mixture_weights, means, scale_diags

        # Compute mixture weights, diagonal factors of covariance matrices and means
        # for each state in parallel. Note that the first dimension of all sufficient
        # statistics is equal to number of states of HMM.
        emission_stats = pytree_sum(batch_stats, axis=0)
        weights, means, scale_diags = vmap(_single_m_step)(
            emission_stats['Sx'], emission_stats['Sxsq'], emission_stats['N'])
        params['weights'] = weights
        params['means'] = means
        params['scale_diags'] = scale_diags
        return params


class GaussianMixtureHMM(HMM):
    """
    Hidden Markov Model with Gaussian mixture emissions.
    Attributes
    ----------
    weights : array, shape (num_states, num_emission_components)
        Mixture weights for each state.
    emission_means : array, shape (num_states, num_emission_components, emission_dim)
        Mean parameters for each mixture component in each state.
    emission_covariance_matrices : array
        Covariance parameters for each mixture components in each state.
    """

    def __init__(self,
                 num_states,
                 num_components,
                 emission_dim,
                 initial_probs_concentration=1.1,
                 transition_matrix_concentration=1.1,
                 emission_weights_concentration=1.1,
                 emission_prior_mean=0.,
                 emission_prior_mean_concentration=1e-4,
                 emission_prior_extra_df=1e-4,
                 emission_prior_scale=0.1):
        self.emission_dim = emission_dim
        self.num_components = num_components
        initial_component = StandardHMMInitialState(num_states, initial_probs_concentration=initial_probs_concentration)
        transition_component = StandardHMMTransitions(num_states, transition_matrix_concentration=transition_matrix_concentration)
        emission_component = GaussianMixtureHMMEmissions(
            num_states, num_components, emission_dim,
            emission_weights_concentration=emission_weights_concentration,
            emission_prior_mean=emission_prior_mean,
            emission_prior_mean_concentration=emission_prior_mean_concentration,
            emission_prior_scale=emission_prior_scale,
            emission_prior_extra_df=emission_prior_extra_df)
        super().__init__(num_states, initial_component, transition_component, emission_component)

    def initialize(self, key=jr.PRNGKey(0),
                   method="prior",
                   initial_probs=None,
                   transition_matrix=None,
                   emission_weights=None,
                   emission_means=None,
                   emission_covariances=None,
                   emissions=None):
        """Initialize the model parameters and their corresponding properties.

        You can either specify parameters manually via the keyword arguments, or you can have
        them set automatically. If any parameters are not specified, you must supply a PRNGKey.
        Parameters will then be sampled from the prior (if `method==prior`).

        Note: in the future we may support more initialization schemes, like K-Means.

        Args:
            key (PRNGKey, optional): random number generator for unspecified parameters. Must not be None if there are any unspecified parameters. Defaults to None.
            method (str, optional): method for initializing unspecified parameters. Currently, only "prior" is allowed. Defaults to "prior".
            initial_probs (array, optional): manually specified initial state probabilities. Defaults to None.
            transition_matrix (array, optional): manually specified transition matrix. Defaults to None.
            emission_weights (array, optional): manually specified emission means. Defaults to None.
            emission_means (array, optional): manually specified emission means. Defaults to None.
            emission_covariances (array, optional): manually specified emission covariances. Defaults to None.
            emissions (array, optional): emissions for initializing the parameters with kmeans. Defaults to None.

        Returns:
            params: a nested dictionary of arrays containing the model parameters.
            props: a nested dictionary of ParameterProperties to specify parameter constraints and whether or not they should be trained.
        """
        if key is not None:
            key1, key2, key3 = jr.split(key , 3)
        else:
            key1 = key2 = key3 = None

        params, props = dict(), dict()
        params["initial"], props["initial"] = self.initial_component.initialize(key1, method=method, initial_probs=initial_probs)
        params["transitions"], props["transitions"] = self.transition_component.initialize(key2, method=method, transition_matrix=transition_matrix)
        params["emissions"], props["emissions"] = self.emission_component.initialize(key3, method=method, emission_weights=emission_weights, emission_means=emission_means, emission_covariances=emission_covariances, emissions=emissions)
        return params, props


class DiagonalGaussianMixtureHMM(HMM):

    def __init__(self,
                 num_states,
                 num_components,
                 emission_dim,
                 initial_probs_concentration=1.1,
                 transition_matrix_concentration=1.1,
                 emission_weights_concentration=1.1,
                 emission_prior_mean=0.,
                 emission_prior_mean_concentration=1e-4,
                 emission_prior_shape=1.,
                 emission_prior_scale=1.):
        """
        Hidden Markov Model with Gaussian mixture emissions where covariance matrices are diagonal.

        Args:
            num_states (_type_): _description_
            num_components (_type_): _description_
            emission_dim (_type_): _description_
            initial_probs_concentration (float, optional): _description_. Defaults to 1.1.
            transition_matrix_concentration (float, optional): _description_. Defaults to 1.1.
            emission_weights_concentration (float, optional): _description_. Defaults to 1.1.
            emission_prior_mean (_type_, optional): _description_. Defaults to 0..
            emission_prior_mean_concentration (_type_, optional): _description_. Defaults to 1e-4.
            emission_prior_shape (_type_, optional): _description_. Defaults to 1..
            emission_prior_scale (_type_, optional): _description_. Defaults to 1..
        """
        self.emission_dim = emission_dim
        self.num_components = num_components
        initial_component = StandardHMMInitialState(num_states, initial_probs_concentration=initial_probs_concentration)
        transition_component = StandardHMMTransitions(num_states, transition_matrix_concentration=transition_matrix_concentration)
        emission_component = GaussianMixtureHMMEmissions(
            num_states, num_components, emission_dim,
            emission_weights_concentration=emission_weights_concentration,
            emission_prior_mean=emission_prior_mean,
            emission_prior_mean_concentration=emission_prior_mean_concentration,
            emission_prior_shape=emission_prior_shape,
            emission_prior_scale=emission_prior_scale)
        super().__init__(num_states, initial_component, transition_component, emission_component)


    def initialize(self, key=jr.PRNGKey(0),
                   method="prior",
                   initial_probs=None,
                   transition_matrix=None,
                   emission_weights=None,
                   emission_means=None,
                   emission_scale_diags=None,
                   emissions=None):
        """Initialize the model parameters and their corresponding properties.

        You can either specify parameters manually via the keyword arguments, or you can have
        them set automatically. If any parameters are not specified, you must supply a PRNGKey.
        Parameters will then be sampled from the prior (if `method==prior`).

        Note: in the future we may support more initialization schemes, like K-Means.

        Args:
            key (PRNGKey, optional): random number generator for unspecified parameters. Must not be None if there are any unspecified parameters. Defaults to None.
            method (str, optional): method for initializing unspecified parameters. Currently, only "prior" is allowed. Defaults to "prior".
            initial_probs (array, optional): manually specified initial state probabilities. Defaults to None.
            transition_matrix (array, optional): manually specified transition matrix. Defaults to None.
            emission_weights (array, optional): manually specified emission means. Defaults to None.
            emission_means (array, optional): manually specified emission means. Defaults to None.
            emission_scale_diags (array, optional): manually specified emission scales (sqrt of the variances). Defaults to None.
            emissions (array, optional): emissions for initializing the parameters with kmeans. Defaults to None.

        Returns:
            params: a nested dictionary of arrays containing the model parameters.
            props: a nested dictionary of ParameterProperties to specify parameter constraints and whether or not they should be trained.
        """
        if key is not None:
            key1, key2, key3 = jr.split(key , 3)
        else:
            key1 = key2 = key3 = None

        params, props = dict(), dict()
        params["initial"], props["initial"] = self.initial_component.initialize(key1, method=method, initial_probs=initial_probs)
        params["transitions"], props["transitions"] = self.transition_component.initialize(key2, method=method, transition_matrix=transition_matrix)
        params["emissions"], props["emissions"] = self.emission_component.initialize(key3, method=method, emission_weights=emission_weights, emission_means=emission_means, emission_scale_diags=emission_scale_diags, emissions=emissions)
        return params, props
