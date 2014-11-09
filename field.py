
import numpy as np
from os import makedirs
from os.path import exists
from numbers import Number
import re

from config import ad, Logger, T
from parallel import pprint, Exchanger
logger = Logger(__name__)
import config, parallel
import BCs
from mesh import extractField, writeField
#import pdb; pdb.set_trace()


class Field(object):
    @staticmethod
    def setSolver(solver):
        Field.solver = solver
        Field.mesh = solver.mesh
    @staticmethod
    def setMesh(mesh):
        Field.mesh = mesh

    def __init__(self, name, field, dimensions):
        self.name = name
        self.field = field
        self.dimensions = dimensions

    @classmethod
    def max(self, a, b):
        return self('max({0},{1})'.format(a.name, b.name), ad.switch(ad.lt(a.field, b.field), b.field, a.field), a.dimensions)

    def info(self):
        assert isinstance(self.field, np.ndarray)
        pprint(self.name + ':', end='')
        fieldMin = np.min(ad.value(self.field))
        fieldMax = np.max(ad.value(self.field))
        assert not np.isnan(fieldMin)
        assert not np.isnan(fieldMax)
        pprint(' min:', fieldMin, 'max:', fieldMax)

    # creates a view
    def component(self, component): 
        assert self.dimensions == (3,)
        return self.__class__('{0}.{1}'.format(self.name, component), self.field[:, component].reshape((-1,1)), (1,))

    def magSqr(self):
        assert self.dimensions == (3,)
        if isinstance(self.field, np.ndarray):
            return self.__class__('magSqr({0})'.format(self.name), np.sum(self.field**2, axis=1).reshape((-1,1)), (1,))
        else:
            return self.__class__('magSqr({0})'.format(self.name), ad.sum(self.field**2, axis=1).reshape((-1,1)), (1,))

    def mag(self):
        return self.magSqr()**0.5

    def abs(self):
        return self.__class__('abs({0})'.format(self.name), ad.abs_(self.field), self.dimensions)

    def dot(self, phi):
        assert self.dimensions[0] == 3
        # if tensor
        if len(self.dimensions) > 1:
            phi = self.__class__(phi.name, phi.field[:,np.newaxis,:], (1,3))
            dimensions = (3,)
        else:
            dimensions = (1,)
        product = ad.sum(self.field * phi.field, axis=-1)
        # if summed over vector
        if len(self.dimensions) == 1:
            product = product.reshape((-1,1))
        return self.__class__('dot({0},{1})'.format(self.name, phi.name), product, dimensions)

    def dotN(self):
        return self.dot(self.mesh.Normals)

    def outer(self, phi):
        return self.__class__('outer({0},{1})'.format(self.name, phi.name), self.field[:,:,np.newaxis] * phi.field[:,np.newaxis,:], (3,3))
    
    # creates a view
    def transpose(self):
        assert len(self.dimensions) == 2
        return self.__class__('{0}.T'.format(self.name), self.field.transpose((0,2,1)), self.dimensions)

    def trace(self):
        assert len(self.dimensions) == 2
        phi = self.field
        return self.__class__('tr({0})'.format(self.name), (phi[:,0,0] + phi[:,1,1] + phi[:,2,2]).reshape((-1,1)), (1,))

    def __neg__(self):
        return self.__class__('-{0}'.format(self.name), -self.field, self.dimensions)

    def __mul__(self, phi):
        if isinstance(phi, Number):
            return self.__class__('{0}*{1}'.format(self.name, phi), self.field * phi, self.dimensions)
        else:
            product = self.field * phi.field
            return self.__class__('{0}*{1}'.format(self.name, phi.name), self.field * phi.field, self.dimensions)


    def __rmul__(self, phi):
        return self * phi

    def __pow__(self, power):
        return self.__class__('{0}**{1}'.format(self.name, power), self.field.__pow__(power), self.dimensions)

    def __add__(self, phi):
        if isinstance(phi, Number):
            return self.__class__('{0}+{1}'.format(self.name, phi), self.field + phi, self.dimensions)
        else:
            return self.__class__('{0}+{1}'.format(self.name, phi.name), self.field + phi.field, self.dimensions)

    def __radd__(self, phi):
        return self.__add__(phi)

    def __sub__(self, phi):
        return self.__add__(-phi)

    def __div__(self, phi):
        return self.__class__('{0}/{1}'.format(self.name, phi.name), self.field / phi.field, self.dimensions)

class CellField(Field):
    def __init__(self, name, field, dimensions, boundary={}, internal=False):
        logger.debug('initializing CellField {0}'.format(name))
        super(self.__class__, self).__init__(name, field, dimensions)
        mesh = self.mesh

        if len(list(boundary.keys())) == 0:
            self.boundary = mesh.defaultBoundary
        else:
            self.boundary = boundary

        if internal:
            if len(dimensions) == 1:
                self.field = ad.alloc(np.float64(0.), *(mesh.nCells, dimensions[0]))
                self.field.tag.test_value = np.zeros((mesh.nCells, dimensions[0]))
            else:
                self.field = ad.alloc(np.float64(0.), *(mesh.nCells, dimensions[0], dimensions[1]))
                self.field.tag.test_value = np.zeros((mesh.nCells, dimensions[0], dimensions[1]))

        self.BC = {}
        for patchID in self.boundary:
            # skip empty patches
            if mesh.boundary[patchID]['nFaces'] == 0:
                continue
            self.BC[patchID] = getattr(BCs, self.boundary[patchID]['type'])(self, patchID)

        if internal:
            self.setInternalField(field)

    @classmethod
    def zeros(self, name, dimensions):
        logger.info('initializing zeros field {0}'.format(name))
        return self(name, ad.zeros((self.mesh.nCells,) + dimensions), internal=False)

    @classmethod
    def copy(self, phi):
        logger.info('copying field {0}'.format(phi.name))
        return self(phi.name, ad.array(ad.value(phi.field).copy()), phi.dimensions, phi.boundary.copy(), internal=False)

    def setInternalField(self, internalField):
        self.field = ad.set_subtensor(self.field[:self.mesh.nInternalCells], internalField)
        self.updateGhostCells()

    def getInternalField(self):
        return self.field[:self.mesh.nInternalCells]

    def updateGhostCells(self):
        logger.info('updating ghost cells for {0}'.format(self.name))
        exchanger = Exchanger()
        for patchID in self.BC:
            if self.boundary[patchID]['type'] in config.processorPatches:
                self.BC[patchID].update(exchanger)
            else:
                self.BC[patchID].update()
        exchanger.wait()

class IOField(Field):
    def __init__(self, name, field, dimensions, boundary={}):
        super(self.__class__, self).__init__(name, field, dimensions)
        logger.debug('initializing IOField {0}'.format(name))
        self.boundary = boundary
        if len(list(boundary.keys())) == 0:
            self.boundary = self.mesh.defaultBoundary
        else:
            self.boundary = boundary

        if not hasattr(self.mesh, 'Normals'):
            self.mesh.Normals = Field('nF', self.mesh.normals, (3,))

    def complete(self):
        logger.debug('completing field {0}'.format(self.name))
        X = ad.dmatrix()
        X.tag.test_value = self.field
        phi = CellField(self.name, X, self.dimensions, self.boundary, internal=True)
        Y = phi.field
        func = T.function([X], Y, on_unused_input='warn')
        self.field = func(self.field)

    @classmethod
    def read(self, name, mesh, time):
        if time.is_integer():
            time = int(time)
        pprint('reading field {0}, time {1}'.format(name, time))
        timeDir = '{0}/{1}/'.format(mesh.case, time)

        content = open(timeDir + name).read()
        foamFile = re.search(re.compile('FoamFile\n{(.*?)}\n', re.DOTALL), content).group(1)
        assert re.search('format[\s\t]+(.*?);', foamFile).group(1) == config.fileFormat
        vector = re.search('class[\s\t]+(.*?);', foamFile).group(1) == 'volVectorField'
        bytesPerField = 8*(1 + 2*vector)
        startBoundary = content.find('boundaryField')
        data = re.search(re.compile('internalField[\s\r\n]+(.*)', re.DOTALL), content[:startBoundary]).group(1)
        internalField = extractField(data, mesh.nInternalCells, vector)
        content = content[startBoundary:]
        boundary = {}
        def getToken(x): 
            token = re.match('[\s\r\n\t]+([a-zA-Z0-9_\.\-\+<>\{\}]+)', x)
            return token.group(1), token.end()
        for patchID in mesh.boundary:
            patch = re.search(re.compile('[\s\r\n\t]+' + patchID + '[\s\r\n]+{', re.DOTALL), content)
            boundary[patchID] = {}
            start = patch.end()
            while 1:
                key, end = getToken(content[start:])
                start += end
                if key == '}':
                    break
                # skip non binary, non value, uniform or empty patches
                elif key == 'value' and config.fileFormat == 'binary' and getToken(content[start:])[0] != 'uniform' and mesh.boundary[patchID]['nFaces'] != 0:
                    match = re.search(re.compile('[ ]+(nonuniform[ ]+List<[a-z]+>[\s\r\n\t0-9]*\()', re.DOTALL), content[start:])
                    nBytes = bytesPerField * mesh.boundary[patchID]['nFaces']
                    start += match.end()
                    prefix = match.group(1)
                    boundary[patchID][key] = prefix + content[start:start+nBytes]
                    start += nBytes
                    match = re.search('\)[\s\r\n\t]*;', content[start:])
                    start += match.end()
                else:
                    match = re.search(re.compile('[ ]+(.*?);', re.DOTALL), content[start:])
                    start += match.end() 
                    boundary[patchID][key] = match.group(1)
        if vector:
            dimensions = (3,)
        else:
            dimensions = (1,)

        return self(name, internalField, dimensions, boundary)

    def write(self, time):
        name = self.name
        field = self.field
        boundary = self.boundary
        mesh = self.mesh
        if time.is_integer():
            time = int(time)
        assert len(field.shape) == 2
        np.set_printoptions(precision=16)
        pprint('writing field {0}, time {1}'.format(name, time))
        timeDir = '{0}/{1}/'.format(mesh.case, time)
        if not exists(timeDir):
            makedirs(timeDir)
        handle = open(timeDir + name, 'w')
        handle.write(config.foamHeader)
        handle.write('FoamFile\n{\n')
        foamFile = config.foamFile.copy()
        foamFile['object'] = name
        if field.shape[1] == 3:
            dtype = 'vector'
            foamFile['class'] = 'volVectorField'
        else:
            dtype = 'scalar'
            foamFile['class'] = 'volScalarField'
        for key in foamFile:
            handle.write('\t' + key + ' ' + foamFile[key] + ';\n')
        handle.write('}\n')
        handle.write('// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n')
        handle.write('dimensions      [0 1 -1 0 0 0 0];\n')
        writeField(handle, field[:mesh.nInternalCells], dtype, 'internalField')
        handle.write('boundaryField\n{\n')
        for patchID in boundary:
            handle.write('\t' + patchID + '\n\t{\n')
            patch = boundary[patchID]
            for attr in patch:
                handle.write('\t\t' + attr + ' ' + patch[attr] + ';\n')
            if patch['type'] in config.valuePatches:
                startFace = mesh.boundary[patchID]['startFace']
                nFaces = mesh.boundary[patchID]['nFaces']
                endFace = startFace + nFaces
                cellStartFace = mesh.nInternalCells + startFace - mesh.nInternalFaces
                cellEndFace = mesh.nInternalCells + endFace - mesh.nInternalFaces
                writeField(handle, field[cellStartFace:cellEndFace], dtype, 'value')
            handle.write('\t}\n')
        handle.write('}\n')
        handle.close()



