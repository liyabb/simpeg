from __future__ import print_function
from . import Utils
from . import Props
from . import DataMisfit
from . import Regularization
from . import mkvc

import properties
import numpy as np
import scipy.sparse as sp
import gc
import logging


class BaseInvProblem(Props.BaseSimPEG):
    """BaseInvProblem(dmisfit, reg, opt)"""

    #: Trade-off parameter
    beta = 1.0

    #: Print debugging information
    debug = False

    #: Set this to a SimPEG.Utils.Counter() if you want to count things
    counter = None

    #: DataMisfit
    dmisfit = None

    #: Regularization
    reg = None

    #: Optimization program
    opt = None

    #: List of strings, e.g. ['_MeSigma', '_MeSigmaI']
    deleteTheseOnModelUpdate = []

    model = Props.Model("Inversion model.")

    @properties.observer('model')
    def _on_model_update(self, value):
        """
            Sets the current model, and removes dependent properties
        """
        for prop in self.deleteTheseOnModelUpdate:
            if hasattr(self, prop):
                delattr(self, prop)

    def __init__(self, dmisfit, reg, opt, **kwargs):
        super(BaseInvProblem, self).__init__(**kwargs)
        assert isinstance(dmisfit, DataMisfit.BaseDataMisfit), 'dmisfit must be a DataMisfit class.'
        assert isinstance(reg, Regularization.BaseRegularization), 'reg must be a Regularization class.'
        self.dmisfit = dmisfit
        self.reg = reg
        self.opt = opt
        self.prob, self.survey = dmisfit.prob, dmisfit.survey
        # TODO: Remove: (and make iteration printers better!)
        self.opt.parent = self
        self.reg.parent = self
        self.dmisfit.parent = self

    @Utils.callHooks('startup')
    def startup(self, m0):
        """startup(m0)

            Called when inversion is first starting.
        """
        if self.debug:
            print('Calling InvProblem.startup')

        if self.reg.mref is None:
            print('SimPEG.InvProblem will set Regularization.mref to m0.')
            self.reg.mref = m0

        self.phi_d = np.nan
        self.phi_m = np.nan

        self.model = m0

        print("""SimPEG.InvProblem is setting bfgsH0 to the inverse of the eval2Deriv.
                    ***Done using same Solver and solverOpts as the problem***""")
        self.opt.bfgsH0 = self.prob.Solver(self.reg.eval2Deriv(self.model), **self.prob.solverOpts)

    @property
    def warmstart(self):
        return getattr(self, '_warmstart', [])

    @warmstart.setter
    def warmstart(self, value):
        assert type(value) is list, 'warmstart must be a list.'
        for v in value:
            assert type(v) is tuple, 'warmstart must be a list of tuples (m, u).'
            assert len(v) == 2, 'warmstart must be a list of tuples (m, u). YOURS IS NOT LENGTH 2!'
            assert isinstance(v[0], np.ndarray), 'first warmstart value must be a model.'
        self._warmstart = value

    def getFields(self, m, store=False, deleteWarmstart=True):
        f = None

        for mtest, u_ofmtest in self.warmstart:
            if m is mtest:
                f = u_ofmtest
                if self.debug:
                    print('InvProb is Warm Starting!')
                break

        if f is None:
            f = self.prob.fields(m)

        if deleteWarmstart:
            self.warmstart = []
        if store:
            self.warmstart += [(m, f)]

        return f

    @Utils.timeIt
    def evalFunction(self, m, return_g=True, return_H=True):
        """evalFunction(m, return_g=True, return_H=True)
        """

        # Log
        logger = logging.getLogger(
            'SimPEG.InvProblem.BaseInversionProblem.evalFunction')
        logger.info('Starting calculations in invProb.evalFunction')
        # Initialize
        self.model = m
        gc.collect()


        f = self.getFields(m, store=(return_g is False and return_H is False))

        logger.debug('Solve the objective function')
        phi_d = self.dmisfit.eval(m, f=f)
        phi_m = self.reg.eval(m)

        # This is a cheap matrix vector calculation.
        self.dpred = self.survey.dpred(m, f=f)

        self.phi_d, self.phi_d_last = phi_d, self.phi_d
        self.phi_m, self.phi_m_last = phi_m, self.phi_m

        phi = phi_d + self.beta * phi_m

        out = (phi,)
        if return_g:
            logger.debug('Solving the objective function gradient')
            phi_dDeriv = self.dmisfit.evalDeriv(m, f=f)
            phi_mDeriv = self.reg.evalDeriv(m)

            g = phi_dDeriv + self.beta * phi_mDeriv
            out += (g,)

        if return_H:
            logger.debug('Solving the objective function Hessian')
            def H_fun(v):
                phi_d2Deriv = self.dmisfit.eval2Deriv(m, v, f=f)
                phi_m2Deriv = self.reg.eval2Deriv(m, v=v)

                return phi_d2Deriv + self.beta * phi_m2Deriv

            H = sp.linalg.LinearOperator( (m.size, m.size), H_fun, dtype=m.dtype )
            out += (H,)
        return out if len(out) > 1 else out[0]


class eachFreq_InvProblem(BaseInvProblem):
    """
    Class aimed at taking advantage of the use of Pardiso Direct solver

    Inherits the BaseInvProblem but extends the evalFunction to extract
    the looping over frequencies/sources for the base functions.

    Assumes to be a FDEM problem

    """

    def __init__(self, dmisfit, reg, opt, **kwargs):
        super(eachFreq_InvProblem, self).__init__(dmisfit, reg, opt, **kwargs)


    @Utils.timeIt
    def evalFunction(self, m, return_g=True, return_sD=True):
        """evalFunction(m, return_g=True, return_sD=True)

        Sets up the evaluation of the objective function,
        gradient and Hessian (if needed).
        """
        # Log
        logger = logging.getLogger(
            'SimPEG.InvProblem.eachFreq_InvProblem.evalFunction')
        logger.info('Starting calculations')
        # Initilize
        # Set model
        self.model = m
        gc.collect()
        # Alias the problem
        problem = self.survey.prob
        problem.model = m
        # Set up the containers
        # Predicted data
        data_pred = problem.dataPair(problem.survey)
        # Observed data
        data_obs = problem.dataPair(problem.survey, self.survey.dobs)
        # Data uncertainty
        data_wd = problem.dataPair(self.survey, self.dmisfit.Wd)
        # Data objective (phi_d)
        data_phi_d = problem.dataPair(self.survey)
        # Gradient multiplecation vector
        data_vec_g = problem.dataPair(self.survey)

        # The Fields
        fields = problem.fieldsPair(problem.mesh, self.survey)
        phi_dDeriv = np.zeros(m.size)

        for freq in self.survey.freqs:
            # Initialize at each loop


            # Factorize
            logger.debug('Working on frequency {:.3e} Hz'.format(freq))
            logger.debug('Factorization starting...')
            A = problem.getA(freq)
            Ainv = problem.Solver(A, *problem.solverOpts)
            logger.debug('Factorization completed')
            # Calculate fields
            logger.debug('Solving fields')
            fields = problem._solve_fields_atFreq(Ainv, freq, fields)
            # Calcualte the residual
            for src in self.survey.getSrcByFreq(freq):
                for rx in src.rxList:
                    data_pred[src, rx] = rx.eval(src, problem.mesh, fields)
                    data_phi_d[src, rx] = data_wd[src, rx] * (data_pred[src, rx] - data_obs[src, rx])
                    data_vec_g[src, rx] = data_wd[src, rx] * data_phi_d[src, rx]
            # Calculate the gradient
            logger.debug('Calculating the phi_dDeriv')
            phi_dDeriv = problem._Jtvec_atFreq(Ainv, freq, data_vec_g, fields, phi_dDeriv)
            # Need to set up the Hessian calculation inside the loop

            Ainv.clean()


        # Calculate the parameters needed
        R = mkvc(data_phi_d)
        phi_d = 0.5*np.vdot(R, R)
        phi_m = self.reg.eval(m)

        # This is a cheap matrix vector calculation.
        self.dpred = self.survey.dpred(m, f=fields)

        self.phi_d, self.phi_d_last = phi_d, self.phi_d
        self.phi_m, self.phi_m_last = phi_m, self.phi_m

        phi = phi_d + self.beta * phi_m

        out = (phi,)
        if return_g:
            logger.debug('Working on the objective function Gradient')
            # Note: phi_dDeriv is calculated in the for loop
            phi_mDeriv = self.reg.evalDeriv(m)

            g = phi_dDeriv + self.beta * phi_mDeriv
            out += (g,)

        if return_sD:

            out += (sD,)
        return out if len(out) > 1 else out[0]
