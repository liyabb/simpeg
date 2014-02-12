from SimPEG import Utils, Solver
from SimPEG.Data import BaseData
from SimPEG.Problem import BaseProblem
from simpegEM.Utils import Sources
from scipy.constants import mu_0
from SimPEG.Utils import sdiag, mkvc
import numpy as np

class DataTDEM1D(BaseData):
    """
        docstring for DataTDEM1D
    """

    txLoc = None #: txLoc
    txType = None #: txType
    rxLoc = None #: rxLoc
    rxType = None #: rxType
    timeCh = None #: timeCh

    def __init__(self, **kwargs):
        BaseData.__init__(self, **kwargs)
        Utils.setKwargs(self, **kwargs)

    def projectFields(self, u):
        return self.Qrx.dot(u.b[:,:,0].T)

    ####################################################
    # Interpolation Matrices
    ####################################################

    @property
    def Qrx(self):
        if self._Qrx is None:
            if self.rxType == 'bz':
                locType = 'fz'
            self._Qrx = self.prob.mesh.getInterpolationMat(self.rxLoc, locType=locType)
        return self._Qrx
    _Qrx = None

class MixinInitialFieldCalc(object):
    """docstring for MixinInitialFieldCalc"""

    def getInitialFields(self):
        if self.data.txType == 'VMD_MVP':
            # Vertical magnetic dipole, magnetic vector potential
            F = self._getInitialFields_VMD_MVP()
        else:
            exStr = 'Invalid txType: ' + str(self.data.txType)
            raise Exception(exStr)
        return F

    def _getInitialFields_VMD_MVP(self):
        if self.mesh._meshType is 'CYL1D':
            MVP = Sources.MagneticDipoleVectorPotential(np.r_[0,0,self.data.txLoc], np.c_[np.zeros(self.mesh.nN), self.mesh.gridN], 'x')
        elif self.mesh._meshType is 'TENSOR':
            MVPx = Sources.MagneticDipoleVectorPotential(self.data.txLoc, self.mesh.gridEx, 'x')
            MVPy = Sources.MagneticDipoleVectorPotential(self.data.txLoc, self.mesh.gridEy, 'y')
            MVPz = Sources.MagneticDipoleVectorPotential(self.data.txLoc, self.mesh.gridEz, 'z')
            MVP = np.concatenate((MVPx, MVPy, MVPz))

        # Initialize field object
        F = FieldsTDEM(self.mesh, 1, self.times.size, 'b')

        # Set initial B
        F.b0 = self.mesh.edgeCurl*MVP

        return F

class MixinTimeStuff(object):
    """docstring for MixinTimeStuff"""

    def dt():
        doc = "Size of time steps"
        def fget(self):
            return self._dt
        def fdel(self):
            del self._dt
        return locals()
    dt = property(**dt())

    def nsteps():
        doc = "Number of steps to take"
        def fget(self):
            return self._nsteps
        def fdel(self):
            del self._nsteps
        return locals()
    nsteps = property(**nsteps())

    def times():
        doc = "Modelling times"
        def fget(self):
            t = np.r_[1:self.nsteps[0]+1]*self.dt[0]
            for i in range(1,self.dt.size):
                t = np.r_[t, np.r_[1:self.nsteps[i]+1]*self.dt[i]+t[-1]]
            return t
        return locals()
    times = property(**times())

    def getDt(self, tInd):
        return np.concatenate([self.dt[i].repeat(self.nsteps[i]) for i in range(self.dt.size)])[tInd]

    def setTimes(self, dt, nsteps):
        dt = np.array(dt)
        nsteps = np.array(nsteps)
        assert dt.size==nsteps.size, "dt, nsteps must be same length"
        self._dt = dt
        self._nsteps = nsteps

class ProblemBaseTDEM(MixinTimeStuff, MixinInitialFieldCalc, BaseProblem):
    """docstring for ProblemTDEM1D"""
    def __init__(self, mesh, model, **kwargs):
        BaseProblem.__init__(self, mesh, model, **kwargs)


    ####################################################
    # Physical Properties
    ####################################################

    @property
    def sigma(self):
        return self._sigma
    @sigma.setter
    def sigma(self, value):
        self._sigma = value
    _sigma = None

    ####################################################
    # Mass Matrices
    ####################################################

    @property
    def MfMui(self): return self._MfMui

    @property
    def MeSigma(self): return self._MeSigma

    @property
    def MeSigmaI(self): return self._MeSigmaI

    def makeMassMatrices(self, m):
        self._MeSigma = self.mesh.getMass(m, loc='e')
        self._MeSigmaI = sdiag(1/self.MeSigma.diagonal())
        self._MfMui = self.mesh.getMass(1/mu_0, loc='f')


    def calcFields(self, sol, solType, tInd):

        if solType == 'b':
            b = sol
            e = self.MeSigmaI*self.mesh.edgeCurl.T*self.MfMui*b
            # Todo: implement non-zero js
        else:
            errStr = 'solType: ' + solType
            raise NotImplementedError(errStr)

        return {'b':b, 'e':e}

    solveOpts = {'factorize':True,'backend':'scipy'}

    def fields(self, m, useThisRhs=None, useThisCalcFields=None):
        RHS = useThisRhs or self.getRHS
        CalcFields = useThisCalcFields or self.calcFields

        self.makeMassMatrices(m)

        F = self.getInitialFields()
        dtFact = None
        for tInd, t in enumerate(self.times):
            dt = self.getDt(tInd)
            if dt!=dtFact:
                dtFact = dt
                A = self.getA(tInd)
                # print 'Factoring...   (dt = ' + str(dt) + ')'
                Asolve = Solver(A, options=self.solveOpts)
                # print 'Done'
            rhs = RHS(tInd, F)
            sol = Asolve.solve(rhs)
            if sol.ndim == 1:
                sol.shape = (sol.size,1)
            newFields = CalcFields(sol, self.solType, tInd)
            F.update(newFields, tInd)
        return F



class FieldsTDEM(object):
    """docstring for FieldsTDEM"""

    phi0 = None #: Initial electric potential
    A0 = None #: Initial magnetic vector potential
    e0 = None #: Initial electric field
    b0 = None #: Initial magnetic flux density
    j0 = None #: Initial current density
    h0 = None #: Initial magnetic field

    phi = None #: Electric potential
    A = None #: Magnetic vector potential
    e = None #: Electric field
    b = None #: Magnetic flux density
    j = None #: Current density
    h = None #: Magnetic field

    def __init__(self, mesh, nTx, nTimes, store):

        self.nTimes = nTimes #: Number of times
        self.nTx = nTx #: Number of transmitters
        self.mesh = mesh

    def update(self, newFields, tInd):
        self.set_b(newFields['b'], tInd)
        self.set_e(newFields['e'], tInd)

    def fieldVec(self):
        u = np.ndarray((0,self.nTx))
        for i in range(self.nTimes):
            u = np.r_[u, self.get_b(i), self.get_e(i)]
        return u

    ####################################################
    # Get Methods
    ####################################################

    def get_b(self, ind):
        if ind == -1:
            return self.b0
        else:
            return self.b[ind,:,:]

    def get_e(self, ind):
        if ind == -1:
            return self.e0
        else:
            return self.e[ind,:,:]

    ####################################################
    # Set Methods
    ####################################################

    def set_b(self, b, ind):
        if self.b is None:
            self.b = np.zeros((self.nTimes, np.sum(self.mesh.nF), self.nTx))
            self.b[:] = np.nan
        self.b[ind,:,:] = b

    def set_e(self, e, ind):
        if self.e is None:
            self.e = np.zeros((self.nTimes, np.sum(self.mesh.nE), self.nTx))
            self.e[:] = np.nan
        self.e[ind,:,:] = e
