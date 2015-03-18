import numpy as np
import time

import config, parallel
from config import ad, T
from parallel import pprint

from field import Field, CellField, IOField
from mesh import Mesh

logger = config.Logger(__name__)

class Solver(object):
    defaultConfig = {
                        'timeIntegrator': 'euler',
                        'source': None,
                        'adjoint': False
                    }

    def __init__(self, case, **userConfig):
        logger.info('initializing solver for {0}'.format(case))
        fullConfig = self.__class__.defaultConfig
        fullConfig.update(userConfig)
        for key in fullConfig:
            setattr(self, key, fullConfig[key])

        self.mesh = Mesh.create(case)
        self.timeIntegrator = globals()[self.timeIntegrator]
        self.padField = PadFieldOp(self)
        self.gradPadField = gradPadFieldOp(self)
        Field.setSolver(self)

    def compile(self):
        pprint('Compiling solver', self.__class__.defaultConfig['timeIntegrator'])
        start = time.time()

        self.dt = T.shared(config.precision(1.))

        #paddedStackedFields = ad.matrix()
        #paddedStackedFields.tag.test_value = np.random.rand(self.mesh.paddedMesh.origMesh.nCells, 5).astype(config.precision)
        stackedFields = ad.matrix()
        paddedStackedFields = self.padField(stackedFields)

        fields = self.unstackFields(paddedStackedFields, CellField)
        fields = self.timeIntegrator(self.equation, self.boundary, fields, self)
        newStackedFields = self.stackFields(fields, ad)
        #self.forward = T.function([paddedStackedFields], [newStackedFields, self.dtc, self.local, self.remote], on_unused_input='warn')#, mode=T.compile.MonitorMode(pre_func=config.inspect_inputs, post_func=config.inspect_outputs))
        self.forward = T.function([stackedFields], [newStackedFields, self.dtc, self.local, self.remote], on_unused_input='warn')#, mode=T.compile.MonitorMode(pre_func=config.inspect_inputs, post_func=config.inspect_outputs))
        if self.adjoint:
            stackedAdjointFields = ad.matrix()
            #paddedGradient = ad.grad(ad.sum(newStackedFields*stackedAdjointFields), paddedStackedFields)
            #self.gradient = T.function([paddedStackedFields, stackedAdjointFields], paddedGradient)
            gradient = ad.grad(ad.sum(newStackedFields*stackedAdjointFields), stackedFields)
            self.gradient = T.function([stackedFields, stackedAdjointFields], gradient)

        end = time.time()
        pprint('Time for compilation:', end-start)
        pprint()

    def stackFields(self, fields, mod): 
        return mod.concatenate([phi.field for phi in fields], axis=1)

    def unstackFields(self, stackedFields, mod, names=None):
        if names is None:
            names = self.names
        fields = []
        nDimensions = np.concatenate(([0], np.cumsum(np.array(self.dimensions))))
        nDimensions = zip(nDimensions[:-1], nDimensions[1:])
        for name, dim, dimRange in zip(names, self.dimensions, nDimensions):
            phi = stackedFields[:, range(*dimRange)]
            fields.append(mod(name, phi, dim))
        return fields


    def run(self, endTime=np.inf, writeInterval=config.LARGE, startTime=0.0, dt=1e-3, nSteps=config.LARGE, 
            mode='simulation', objective=lambda x: 0, perturb=None):

        logger.info('running solver for {0}'.format(nSteps))
        mesh = self.mesh
        #initialize
        fields = self.initFields(startTime)
        pprint()

        if not hasattr(self, 'forward'):
            self.compile()

        t = startTime
        dts = dt
        timeIndex = 0
        if isinstance(dts, np.ndarray):
            self.dt.set_value(config.precision(dts[timeIndex]))
        else:
            self.dt.set_value(config.precision(dts))
        stackedFields = self.stackFields(fields, np)
        
        timeSteps = []
        # objective is local
        if perturb is not None:
            perturb(stackedFields, t)
        result = objective(stackedFields)
        # writing and returning local solutions
        if mode == 'forward':
            solutions = [stackedFields]

        while t < endTime and timeIndex < nSteps:
            start = time.time()

            pprint('Time marching for', ' '.join(self.names))
            for index in range(0, len(fields)):
                fields[index].info()

            # mpi stuff, bloat stackedFields
            #TESTING
            #stackedFields = parallel.getRemoteCells(stackedFields, mesh)  

            pprint('Time step', timeIndex)
            #stackedFields, dtc = self.forward(stackedFields)
            stackedFields, dtc, local, remote = self.forward(stackedFields)
            #print local.shape, local.dtype, local, np.abs(local).max(), np.abs(local).min()
            #print remote.shape, remote.dtype, remote, np.abs(remote).max(), np.abs(remote).min()

            #lStart = 0
            #rStart = 0
            #print parallel.rank, mesh.remotePatches
            #for patchID in mesh.remotePatches:
            #    n = mesh.boundary[patchID]['nFaces']
            #    #n = len(mesh.paddedMesh.localRemoteFaces[self.loc][patchID])
            #    np.savetxt('local_' + patchID, local[lStart:lStart+n])
            #    #print 'local', patchID, local[lStart:lStart+n], local.shape
            #    lStart += n
            #    #n = mesh.paddedMesh.remoteFaces[self.loc][patchID]
            #    np.savetxt('remote_' + patchID, remote[rStart:rStart+n])
            #    #print 'remote', patchID, remote[rStart:rStart+n], remote.shape
            #    rStart += n


            fields = self.unstackFields(stackedFields, IOField)
            # TODO: fix unstacking F_CONTIGUOUS
            for phi in fields:
                phi.field = np.ascontiguousarray(phi.field)

            end = time.time()
            pprint('Time for iteration:', end-start)
            
            result += objective(stackedFields)
            dt = self.dt.get_value()
            timeSteps.append([t, dt])
            if mode == 'forward':
                solutions.append(stackedFields)

            t = round(t+dt, 9)
            timeIndex += 1
            pprint('Simulation Time:', t, 'Time step:', dt)
            if timeIndex % writeInterval == 0:
                self.writeFields(fields, t)
            pprint()

            # compute dt for next time step
            dt = min(parallel.min(dtc), dt*self.stepFactor, endTime-t)
            if isinstance(dts, np.ndarray):
                self.dt.set_value(config.precision(dts[timeIndex]))
            else:
                self.dt.set_value(config.precision(dt))

        if mode == 'forward':
            return solutions
        if (timeIndex % writeInterval != 0) and (timeIndex >= writeInterval):
            self.writeFields(fields, t)
        return timeSteps, result

def euler(equation, boundary, fields, solver):
    LHS = equation(*fields)
    internalFields = [(fields[index].getInternalField() - LHS[index].field*solver.dt) for index in range(0, len(fields))]
    newFields = boundary(*internalFields)
    return newFields

def RK2(equation, boundary, fields, solver):
    return fields

def RK(equation, boundary, fields, solver):
    def NewFields(a, LHS):
        internalFields = [phi.getInternalField() for phi in fields]
        for termIndex in range(0, len(a)):
            for index in range(0, len(fields)):
                internalFields[index] -= a[termIndex]*LHS[termIndex][index].field*solver.dt
        return boundary(*internalFields)

    def f(a, *LHS):
        if len(LHS) != 0:
            newFields = NewFields(a, LHS)
        else:
            newFields = fields
        return equation(*newFields)

    k1 = f([0.])
    k2 = f([0.5], k1)
    k3 = f([0.5], k2)
    k4 = f([1.], k3)
    newFields = NewFields([1./6, 1./3, 1./3, 1./6], [k1, k2, k3, k4])

    return newFields

# DOES NOT WORK
def implicit(equation, boundary, fields, garbage):
    assert ad.__name__ == 'numpad'
    start = time.time()

    names = [phi.name for phi in fields]
    pprint('Solving for', ' '.join(names))
    for index in range(0, len(fields)):
        fields[index].old = CellField.copy(fields[index])
        fields[index].info()
    nDimensions = np.concatenate(([0], np.cumsum(np.array([phi.dimensions[0] for phi in fields]))))
    nDimensions = zip(nDimensions[:-1], nDimensions[1:])
    def setInternalFields(stackedInternalFields):
        internalFields = []
        # range creates a copy on the array
        for index in range(0, len(fields)):
            internalFields.append(stackedInternalFields[:, range(*nDimensions[index])])
        return boundary(*internalFields)
    def solver(internalFields):
        newFields = setInternalFields(internalFields)
        for index in range(0, len(fields)):
            newFields[index].old = fields[index].old
        return ad.hstack([phi.field for phi in equation(*newFields)])

    internalFields = ad.hstack([phi.getInternalField() for phi in fields])
    solution = ad.solve(solver, internalFields)
    newFields = setInternalFields(solution)
    for index in range(0, len(fields)):
        newFields[index].name = fields[index].name

    end = time.time()
    pprint('Time for iteration:', end-start)
    return newFields

class PadFieldOp(T.Op):
    __props__ = ()

    def __init__(self, solver):
        self.solver = solver
        self.mesh = solver.mesh

    def make_node(self, x):
        assert hasattr(self, '_props')
        x = ad.as_tensor_variable(x)
        return T.Apply(self, [x], [x.type()])

    def perform(self, node, inputs, output_storage):
        assert inputs[0].flags['C_CONTIGUOUS'] == True
        output_storage[0][0] = parallel.getRemoteCells(inputs[0], self.mesh)

    def grad(self, inputs, output_grads):
        return [self.solver.gradPadField(output_grads[0])]

class gradPadFieldOp(T.Op):
    __props__ = ()

    def __init__(self, solver):
        self.solver = solver
        self.mesh = solver.mesh

    def make_node(self, x):
        assert hasattr(self, '_props')
        x = ad.as_tensor_variable(x)
        return T.Apply(self, [x], [x.type()])

    def perform(self, node, inputs, output_storage):
        #assert inputs[0].flags['C_CONTIGUOUS'] == True
        #output_storage[0][0] = parallel.getAdjointRemoteCells(inputs[0], self.mesh)
        output_storage[0][0] = parallel.getAdjointRemoteCells(np.ascontiguousarray(inputs[0]), self.mesh)
