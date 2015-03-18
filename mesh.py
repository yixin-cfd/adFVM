import numpy as np
from scipy import sparse as sp
import re
import time
import copy

import config, parallel
from config import ad, adsparse, T
from parallel import pprint, Exchanger

logger = config.Logger(__name__)

class Mesh(object):
    constants = ['nInternalFaces',
                 'nFaces',
                 'nBoundaryFaces',
                 'nInternalCells',
                 'nGhostCells',
                 'nCells',
                 'nLocalCells']
                 
    fields = ['owner', 'neighbour',
              'areas', 'volumes',
              'weights', 'deltas', 'normals',
              'cellCentres', 'faceCentres',
              'boundary']

    def __init__(self):
        for attr in Mesh.constants:
            setattr(self, attr, 0)
        for attr in Mesh.fields:
            setattr(self, attr, np.array([]))

        #self.localRemoteCells = None
        #self.localRemoteFaces = None
        #self.remoteCells = None
        #self.remoteFaces = None

    @classmethod
    def copy(cls, mesh, constants=True, fields=False):
        self = cls()
        for attr in cls.fields:
            if fields or attr == 'boundary':
              setattr(self, attr, copy.deepcopy(getattr(mesh, attr)))
        if constants:
            for attr in cls.constants:
              setattr(self, attr, getattr(mesh, attr))
        return self

    @classmethod
    def create(cls, caseDir=None):
        start = time.time()
        pprint('Reading mesh')
        self = cls()

        self.case = caseDir + parallel.processorDirectory
        meshDir = self.case + 'constant/polyMesh/'
        self.faces = self.read(meshDir + 'faces', np.int32)
        self.points = self.read(meshDir + 'points', np.float64).astype(config.precision)
        self.owner = self.read(meshDir + 'owner', np.int32).ravel()
        self.neighbour = self.read(meshDir + 'neighbour', np.int32).ravel()
        self.boundary, self.localPatches, self.remotePatches = self.readBoundary(meshDir + 'boundary')
        self.origPatches = copy.copy(self.localPatches)
        self.origPatches.sort()
        self.defaultBoundary = self.getDefaultBoundary()
        self.calculatedBoundary = self.getCalculatedBoundary()

        self.nInternalFaces = len(self.neighbour)
        self.nFaces = len(self.owner)
        self.nBoundaryFaces = self.nFaces-self.nInternalFaces
        self.nInternalCells = np.max(self.owner)+1
        self.nGhostCells = self.nBoundaryFaces
        self.nCells = self.nInternalCells + self.nGhostCells

        self.normals = self.getNormals()
        self.faceCentres, self.areas = self.getFaceCentresAndAreas()
        # uses neighbour
        self.cellFaces = self.getCellFaces()     # nInternalCells
        self.cellCentres, self.volumes = self.getCellCentresAndVolumes() # nCells after ghost cell mod
        # uses neighbour
        self.sumOp = self.getSumOp(self)             # (nInternalCells, nFaces)
        
        # ghost cell modification
        self.nLocalCells = self.createGhostCells()
        self.deltas = self.getDeltas()           # nFaces
        self.weights = self.getWeights()   # nFaces

        # padded mesh
        self.paddedMesh = cls.createPaddedMesh(self)
        # theano shared variables
        self.origMesh = cls.copy(self, fields=True)
        self.makeShared()

        end = time.time()
        pprint('Time for reading mesh:', end-start)
        pprint()

        return self

    def read(self, foamFile, dtype):
        logger.info('read {0}'.format(foamFile))
        content = open(foamFile).read()
        foamFileDict = re.search(re.compile('FoamFile\n{(.*?)}\n', re.DOTALL), content).group(1)
        assert re.search('format[\s\t]+(.*?);', foamFileDict).group(1) == config.fileFormat
        start = content.find('(') + 1
        end = content.rfind(')')
        if config.fileFormat == 'binary':
            if foamFile[-5:] == 'faces':
                nFaces1 = int(re.search('[0-9]+', content[start-2:0:-1]).group(0)[::-1])
                endIndices = start + nFaces1*4
                faceIndices = np.fromstring(content[start:endIndices], dtype)
                faceIndices = faceIndices[1:] - faceIndices[:-1]
                startData = content.find('(', endIndices) + 1
                data = np.fromstring(content[startData:end], dtype)
                nCellFaces = faceIndices[0] 
                return np.hstack((faceIndices.reshape(-1, 1), data.reshape(len(data)/nCellFaces, nCellFaces)))
            else:
                data = np.fromstring(content[start:end], dtype)
                if foamFile[-6:] == 'points':
                    data = data.reshape(len(data)/3, 3)
                return data
        else:
            f = lambda x: list(filter(None, re.split('[ ()\n]+', x)))
            return np.array(list(map(f, filter(None, re.split('\n', content[start:end])))), dtype)

    def readBoundary(self, boundaryFile):
        logger.info('read {0}'.format(boundaryFile))
        content = removeCruft(open(boundaryFile).read())
        patches = re.findall(re.compile('([A-Za-z0-9_]+)[\r\s\n]+{(.*?)}', re.DOTALL), content)
        boundary = {}
        localPatches = []
        remotePatches = []
        for patch in patches:
            boundary[patch[0]] = dict(re.findall('\n[ \t]+([a-zA-Z]+)[ ]+(.*?);', patch[1]))
            boundary[patch[0]]['nFaces'] = int(boundary[patch[0]]['nFaces'])
            boundary[patch[0]]['startFace'] = int(boundary[patch[0]]['startFace'])
            if boundary[patch[0]]['type'] in config.processorPatches:
                remotePatches.append(patch[0])
            else:
                localPatches.append(patch[0])
        return boundary, localPatches, remotePatches

    def getNormals(self):
        logger.info('generated normals')
        v1 = self.points[self.faces[:,1]]-self.points[self.faces[:,2]]
        v2 = self.points[self.faces[:,2]]-self.points[self.faces[:,3]]
        # CROSS product makes it F_CONTIGUOUS even if normals is not
        normals = np.cross(v1, v2)
        # change back to contiguous
        normals = np.ascontiguousarray(normals)
        return normals / config.norm(normals, axis=1, keepdims=True)

    def getCellFaces(self):
        logger.info('generated cell faces')
        enum = lambda x: np.column_stack((np.indices(x.shape, np.int32)[0], x)) 
        combined = np.concatenate((enum(self.owner), enum(self.neighbour)))
        cellFaces = combined[combined[:,1].argsort(), 0]
        # todo: make it a list ( investigate np.diff )
        return cellFaces.reshape(self.nInternalCells, len(cellFaces)/self.nInternalCells)

    def getCellCentresAndVolumes(self):
        logger.info('generated cell centres and volumes')
        nCellFaces = self.cellFaces.shape[1]
        cellCentres = np.mean(self.faceCentres[self.cellFaces], axis=1)
        sumCentres = cellCentres*0
        sumVolumes = np.sum(sumCentres, axis=1, keepdims=True)
        areaNormals = self.areas * self.normals
        for index in range(0, nCellFaces):
            indices = self.cellFaces[:,index]
            height = cellCentres-self.faceCentres[indices]
            volumes = np.abs(np.sum(areaNormals[indices]*height, axis=1, keepdims=True))/3
            centres = (3./4)*self.faceCentres[indices] + (1./4)*cellCentres
            sumCentres += volumes * centres
            sumVolumes += volumes
        cellCentres = sumCentres/sumVolumes
        return cellCentres, sumVolumes

    def getFaceCentresAndAreas(self):
        logger.info('generated face centres and areas')
        nFacePoints = self.faces[0, 0]
        faceCentres = np.mean(self.points[self.faces[:,1:]], axis=1)
        sumAreas = 0
        sumCentres = 0
        for index in range(1, nFacePoints+1):
            points = self.points[self.faces[:, index]]
            nextPoints = self.points[self.faces[:, (index % nFacePoints)+1]]
            centres = (points + nextPoints + faceCentres)/3
            normals = np.cross((nextPoints - points), (faceCentres - points))
            areas = config.norm(normals, axis=1, keepdims=True)/2
            sumAreas += areas
            sumCentres += areas*centres
        faceCentres = sumCentres/sumAreas
        return faceCentres, sumAreas

    def getDeltas(self):
        logger.info('generated deltas')
        P = self.cellCentres[self.owner]
        N = self.cellCentres[self.neighbour]
        return config.norm(P-N, axis=1, keepdims=True)

    def getWeights(self):
        logger.info('generated face deltas')
        P = self.cellCentres[self.owner]
        N = self.cellCentres[self.neighbour]
        F = self.faceCentres
        neighbourDist = np.abs(np.sum((F-N)*self.normals, axis=1, keepdims=True))
        ownerDist = np.abs(np.sum((F-P)*self.normals, axis=1, keepdims=True))
        weights = neighbourDist/(neighbourDist + ownerDist)
        return weights

    def getSumOp(self, mesh, ghost=False):
        logger.info('generated sum op')
        owner = sp.csc_matrix((np.ones(mesh.nFaces, config.precision), mesh.owner, np.arange(0, mesh.nFaces+1, dtype=np.int32)), shape=(mesh.nInternalCells, mesh.nFaces))
        Nindptr = np.concatenate((np.arange(0, mesh.nInternalFaces+1, dtype=np.int32), mesh.nInternalFaces*np.ones(mesh.nFaces-mesh.nInternalFaces, np.int32)))
        neighbour = sp.csc_matrix((-np.ones(mesh.nInternalFaces, config.precision), mesh.neighbour[:mesh.nInternalFaces], Nindptr), shape=(mesh.nInternalCells, mesh.nFaces))
        # skip empty patches
        #for patchID in self.boundary:
        #    patch = self.boundary[patchID]
        #    if patch['type'] == 'empty' and patch['nFaces'] != 0:
        #        pprint('Deleting empty patch ', patchID)
        #        startFace = mesh.nInternalFaces + patch['startFace'] - self.nInternalFaces
        #        endFace = startFace + patch['nFaces']
        #        owner.data[startFace:endFace] = 0
        sumOp = (owner + neighbour).tocsr()
    
        # different faces, same owner repeat fix
        nFaces = 6
        repeat = [[-1]*nFaces]
        if ghost:
            correction = sp.lil_matrix((mesh.nInternalCells, mesh.nInternalCells), dtype=config.precision)
            correction.setdiag(np.ones(mesh.nInternalCells, config.precision))
            internalCursor = self.nInternalCells
            for patchID in self.remotePatches:
                internal = mesh.remoteCells['internal'][patchID]
                uniqueInternal, inverse = np.unique(internal, return_inverse=True)
                repeaters = np.where(np.bincount(inverse) > 1)
                for index in uniqueInternal[repeaters]:
                    indices = internalCursor + np.where(internal == index)[0]
                    #print indices, patchID, index
                    for i,j in enumerate(indices):
                        # rotate and pad this
                        padded = -np.ones(nFaces, dtype=np.int32)
                        padded[:len(indices)] = np.roll(indices, -i)
                        repeat.append(padded)
                        correction[indices, j] = 1.

                internalCursor += len(internal)
            sumOp = (correction.tocsr())*sumOp
        mesh.repeat = T.shared(np.array(repeat, dtype=np.int32).T)

        return adsparse.CSR(sumOp.data, sumOp.indices, sumOp.indptr, sumOp.shape)

    def getDefaultBoundary(self):
        logger.info('generated default boundary')
        boundary = {}
        for patchID in self.boundary:
            boundary[patchID] = {}
            if self.boundary[patchID]['type'] in config.defaultPatches:
                boundary[patchID]['type'] = self.boundary[patchID]['type']
            else:
                boundary[patchID]['type'] = 'zeroGradient'
        return boundary

    def getCalculatedBoundary(self):
        logger.info('generated calculated boundary')
        boundary = {}
        for patchID in self.boundary:
            boundary[patchID] = {}
            if self.boundary[patchID]['type'] in config.coupledPatches:
                boundary[patchID]['type'] = self.boundary[patchID]['type']
            else:
                boundary[patchID]['type'] = 'calculated'
        return boundary

    def getProcessorPatchInfo(self, patchID):
        patch = self.boundary[patchID]
        local = patch['myProcNo']
        remote = patch['neighbProcNo']
        tag = 0
        if patch['type'] == 'processorCyclic':
            commonPatch = patch['referPatch']
            if local > remote:
                commonPatch = self.boundary[commonPatch]['neighbourPatch']
            tag = 1 + self.origPatches.index(commonPatch)
        return local, remote, tag

    def createGhostCells(self):
        logger.info('generated ghost cells')
        self.neighbour = np.concatenate((self.neighbour, np.zeros(self.nBoundaryFaces, np.int32)))
        self.cellCentres = np.concatenate((self.cellCentres, np.zeros((self.nBoundaryFaces, 3), config.precision)))
        nLocalCells = self.nInternalCells
        exchanger = Exchanger()
        for patchID in self.boundary:
            patch = self.boundary[patchID]
            startFace = patch['startFace']
            nFaces = patch['nFaces']
            # empty patches
            if nFaces == 0:
                continue
            elif patch['type'] not in config.processorPatches:
                nLocalCells += nFaces
            endFace = startFace + nFaces
            cellStartFace = self.nInternalCells + startFace - self.nInternalFaces
            cellEndFace = self.nInternalCells + endFace - self.nInternalFaces
            # append neighbour
            self.neighbour[startFace:endFace] = range(cellStartFace, cellEndFace)
            if patch['type'] == 'cyclic': 
                neighbourPatch = self.boundary[patch['neighbourPatch']]   
                neighbourStartFace = neighbourPatch['startFace']
                neighbourEndFace = neighbourStartFace + nFaces
                # apply transformation: single value
                # append cell centres
                patch['transform'] = self.faceCentres[startFace]-self.faceCentres[neighbourStartFace]
                self.cellCentres[cellStartFace:cellEndFace] = patch['transform'] + self.cellCentres[self.owner[neighbourStartFace:neighbourEndFace]]

            elif patch['type'] == 'processor':
                patch['neighbProcNo'] = int(patch['neighbProcNo'])
                patch['myProcNo'] = int(patch['myProcNo'])
                local, remote, tag = self.getProcessorPatchInfo(patchID)
                # exchange data
                exchanger.exchange(remote, self.cellCentres[self.owner[startFace:endFace]], self.cellCentres[cellStartFace:cellEndFace], tag)

            elif patch['type'] == 'processorCyclic':
                patch['neighbProcNo'] = int(patch['neighbProcNo'])
                patch['myProcNo'] = int(patch['myProcNo'])
                local, remote, tag = self.getProcessorPatchInfo(patchID)
                # apply transformation
                exchanger.exchange(remote, -self.faceCentres[startFace:endFace] + self.cellCentres[self.owner[startFace:endFace]], self.cellCentres[cellStartFace:cellEndFace], tag)
            else:
                # append cell centres
                self.cellCentres[cellStartFace:cellEndFace] = self.faceCentres[startFace:endFace]
        exchanger.wait()
        for patchID in self.boundary:
            patch = self.boundary[patchID]
            startFace = patch['startFace']
            nFaces = patch['nFaces']
            # empty patches
            if nFaces == 0:
                continue
            endFace = startFace + nFaces
            cellStartFace = self.nInternalCells + startFace - self.nInternalFaces
            cellEndFace = self.nInternalCells + endFace - self.nInternalFaces

            if patch['type'] == 'processorCyclic':
                self.cellCentres[cellStartFace:cellEndFace] += self.faceCentres[startFace:endFace]

        return nLocalCells

    @classmethod
    def createPaddedMesh(cls, self):
        logger.info('generated padded mesh')
        if parallel.nProcessors == 1:
            return self
        mesh = cls()
        # set correct values for faces whose neighbours are ghost cells
        nLocalBoundaryFaces = self.nLocalCells - self.nInternalCells
        nLocalRemoteBoundaryFaces = self.nCells - self.nLocalCells
        nLocalFaces = self.nInternalFaces + nLocalBoundaryFaces

        # processor patches are in increasing order
        remoteInternal = {'mapping':{},'owner':{}, 'neighbour':{}, 'areas':{}, 'weights':{}, 'normals':{}, 'volumes':{}}
        remoteBoundary = copy.deepcopy(remoteInternal)
        remoteExtra = {}
        mesh.localRemoteCells = {'internal':{}, 'boundary':{}, 'extra':{}}
        mesh.localRemoteFaces = copy.deepcopy(mesh.localRemoteCells)
        mesh.remoteCells = copy.deepcopy(mesh.localRemoteCells)
        mesh.remoteFaces = copy.deepcopy(mesh.localRemoteCells)
        localNormals = copy.deepcopy(mesh.localRemoteCells)
        localWeights = copy.deepcopy(mesh.localRemoteCells)
        localOwner = copy.deepcopy(mesh.localRemoteCells)
        localNeighbour = copy.deepcopy(mesh.localRemoteCells)
        exchanger = Exchanger()
        for patchID in self.remotePatches:
            patch = self.boundary[patchID]
            startFace = patch['startFace']
            nFaces = patch['nFaces']
            endFace = startFace + nFaces
            cellStartFace = self.nInternalCells + startFace - nLocalFaces
            cellEndFace = self.nInternalCells + endFace - nLocalFaces
            
            # extraInternalCells might not be unique
            #extraInternalCells = np.unique(self.owner[startFace:endFace])
            extraInternalCells = self.owner[startFace:endFace]
            extraFaces = self.cellFaces[extraInternalCells].ravel()
            boundaryFaces = range(startFace, endFace)
            extraFaces  = np.setdiff1d(extraFaces, boundaryFaces)
            owner = self.owner[extraFaces]
            neighbour = self.neighbour[extraFaces]
            normals = self.normals[extraFaces]
            weights = self.weights[extraFaces]
            extraCells = np.concatenate((owner, neighbour))
            extraGhostCells = np.setdiff1d(extraCells, extraInternalCells)
            # extra = localRemote
            # check cells on another processor
            extraIndex = (extraGhostCells >= self.nLocalCells)
            extraRemoteCells = extraGhostCells[extraIndex]
            # rearrange extraGhostCells 
            extraGhostCells = np.concatenate((extraGhostCells[np.invert(extraIndex)], extraRemoteCells))
            if len(extraRemoteCells) > 0:
                print 'Extra remote ghost cells:', patchID, len(extraRemoteCells)

            boundaryIndex = np.in1d(neighbour, extraGhostCells)
            # swap extra boundary faces whose owner is wrong
            swapIndex = np.in1d(owner, extraGhostCells)
            tmp = neighbour[swapIndex]
            neighbour[swapIndex] = owner[swapIndex]
            owner[swapIndex] = tmp
            ## flip normals and invert weights
            normals[swapIndex] *= -1
            weights[swapIndex] = 1-weights[swapIndex]

            boundaryIndex = np.in1d(neighbour, extraGhostCells)
            internalIndex = np.invert(boundaryIndex)
            extraBoundaryFaces = extraFaces[boundaryIndex]
            extraInternalFaces = extraFaces[internalIndex]

            localNormals['internal'][patchID] = normals[internalIndex]
            localNormals['boundary'][patchID] = normals[boundaryIndex]
            localWeights['internal'][patchID] = weights[internalIndex]
            localWeights['boundary'][patchID] = weights[boundaryIndex]
            localOwner['internal'][patchID] = owner[internalIndex]
            localOwner['boundary'][patchID] = owner[boundaryIndex]
            localNeighbour['internal'][patchID] = neighbour[internalIndex]
            localNeighbour['boundary'][patchID] = neighbour[boundaryIndex]

            mesh.localRemoteCells['internal'][patchID] = extraInternalCells
            mesh.localRemoteCells['boundary'][patchID] = extraGhostCells
            mesh.localRemoteCells['extra'][patchID] = extraRemoteCells
            mesh.localRemoteFaces['internal'][patchID] = extraInternalFaces
            mesh.localRemoteFaces['boundary'][patchID] = extraBoundaryFaces

            # exchange sizes
            local, remote, tag = self.getProcessorPatchInfo(patchID)
            tag = {0:tag}
            tagIncrement = len(self.origPatches) + 1

            def remoteExchange(sendData, buff):
                buff[patchID] = np.zeros(1, int)
                sendData = np.array([sendData])
                exchanger.exchange(remote, sendData, buff[patchID], tag[0])
                tag[0] += tagIncrement

            remoteExchange(len(extraInternalCells), mesh.remoteCells['internal'])
            remoteExchange(len(extraGhostCells), mesh.remoteCells['boundary'])
            remoteExchange(len(extraRemoteCells), mesh.remoteCells['extra'])
            remoteExchange(len(extraInternalFaces), mesh.remoteFaces['internal'])
            remoteExchange(len(extraBoundaryFaces), mesh.remoteFaces['boundary'])

        exchanger.wait()

        exchanger = Exchanger()
        for patchID in self.remotePatches:

            local, remote, tag = self.getProcessorPatchInfo(patchID)
            tag = {0:tag}
            tagIncrement = len(self.origPatches) + 1

            def remoteExchange(field, sendData, size, location):
                order = 'C'
                size = (size[0], ) + sendData.shape[1:]
                if location == 'internal':
                    remoteInternal[field][patchID] = np.zeros(size, sendData.dtype, order)
                    #print field, sendData.flags['F_CONTIGUOUS'], remoteInternal[field][patchID].flags['F_CONTIGUOUS']
                    exchanger.exchange(remote, sendData, remoteInternal[field][patchID], tag[0])
                elif location == 'boundary':
                    remoteBoundary[field][patchID] = np.zeros(size, sendData.dtype, order)
                    exchanger.exchange(remote, sendData, remoteBoundary[field][patchID], tag[0])
                else:
                    remoteExtra[patchID] = np.zeros(size, sendData.dtype, order)
                    exchanger.exchange(remote, sendData, remoteExtra[patchID], tag[0])
                tag[0] += tagIncrement


            #0: send extraInternalCells, first layer mapping
            remoteExchange('mapping', mesh.localRemoteCells['internal'][patchID], mesh.remoteCells['internal'][patchID], 'internal')

            #2: send extraGhostCells, second layer mapping
            remoteExchange('mapping', mesh.localRemoteCells['boundary'][patchID], mesh.remoteCells['boundary'][patchID], 'boundary')

            # extraRemoteCells
            remoteExchange('extra', mesh.localRemoteCells['extra'][patchID], mesh.remoteCells['extra'][patchID], 'extra')

            #6: send owner and neighbour and do the mapping
            remoteExchange('owner', localOwner['internal'][patchID], mesh.remoteFaces['internal'][patchID], 'internal')
            remoteExchange('owner', localOwner['boundary'][patchID], mesh.remoteFaces['boundary'][patchID],'boundary')
            remoteExchange('neighbour', localNeighbour['internal'][patchID], mesh.remoteFaces['internal'][patchID], 'internal')
            remoteExchange('neighbour', localNeighbour['boundary'][patchID], mesh.remoteFaces['boundary'][patchID], 'boundary')

            #14: send rest
            remoteExchange('areas', self.areas[mesh.localRemoteFaces['internal'][patchID]], mesh.remoteFaces['internal'][patchID], 'internal')
            remoteExchange('areas', self.areas[mesh.localRemoteFaces['boundary'][patchID]], mesh.remoteFaces['boundary'][patchID], 'boundary')
            # WHY THE FUCK IS normals F_CONTIGUOUS
            remoteExchange('normals', localNormals['internal'][patchID], mesh.remoteFaces['internal'][patchID], 'internal')
            remoteExchange('normals', localNormals['boundary'][patchID], mesh.remoteFaces['boundary'][patchID], 'boundary')
            remoteExchange('weights', localWeights['internal'][patchID], mesh.remoteFaces['internal'][patchID], 'internal')
            remoteExchange('weights', localWeights['boundary'][patchID], mesh.remoteFaces['boundary'][patchID], 'boundary')
            remoteExchange('volumes', self.volumes[mesh.localRemoteCells['internal'][patchID]], mesh.remoteCells['internal'][patchID], 'internal')
            
        statuses = exchanger.wait()

        nRemoteInternalFaces = 0
        nRemoteBoundaryFaces = 0
        for index, patchID in enumerate(self.remotePatches):
            mesh.remoteCells['internal'][patchID] = remoteInternal['mapping'][patchID]
            mesh.remoteCells['boundary'][patchID] = remoteBoundary['mapping'][patchID]
            mesh.remoteCells['extra'][patchID] = remoteExtra[patchID]
            mesh.remoteFaces['internal'][patchID] = remoteInternal['owner'][patchID]
            mesh.remoteFaces['boundary'][patchID] = remoteBoundary['owner'][patchID]
            nRemoteInternalFaces += len(remoteInternal['owner'][patchID])
            nRemoteBoundaryFaces += len(remoteBoundary['owner'][patchID])

        mesh.nInternalFaces = self.nInternalFaces + nLocalRemoteBoundaryFaces + nRemoteInternalFaces
        mesh.nBoundaryFaces = nLocalBoundaryFaces + nRemoteBoundaryFaces
        mesh.nFaces = mesh.nInternalFaces + mesh.nBoundaryFaces
        mesh.nInternalCells = self.nInternalCells + nLocalRemoteBoundaryFaces
        mesh.nGhostCells = mesh.nBoundaryFaces
        mesh.nCells = mesh.nInternalCells + mesh.nGhostCells

        nLocalInternalFaces = self.nInternalFaces + nLocalRemoteBoundaryFaces
        remoteGhostStartFace = mesh.nInternalFaces + nLocalBoundaryFaces
        def padFaceField(fieldName):
            field = getattr(self, fieldName)
            example = remoteInternal[fieldName][self.remotePatches[0]]
            size = (mesh.nFaces, ) + example.shape[1:]
            dtype = example.dtype

            # padded face field structure:
            # internal, proc boundary, remote internal, local boundary, remote boundary

            faceField = np.zeros(size, dtype)
            faceField[:self.nInternalFaces] = field[:self.nInternalFaces]
            faceField[self.nInternalFaces:nLocalInternalFaces] = field[nLocalFaces:]
            faceField[mesh.nInternalFaces:remoteGhostStartFace] = field[self.nInternalFaces:nLocalFaces]
            internalCursor = nLocalInternalFaces
            boundaryCursor = remoteGhostStartFace
            for patchID in self.remotePatches:
                nInternalFaces = len(remoteInternal['owner'][patchID])
                faceField[internalCursor:internalCursor + nInternalFaces] = remoteInternal[fieldName][patchID][:nInternalFaces]
                internalCursor += nInternalFaces
                nBoundaryFaces = len(remoteBoundary['owner'][patchID])
                faceField[boundaryCursor:boundaryCursor + nBoundaryFaces] = remoteBoundary[fieldName][patchID][:nBoundaryFaces]
                boundaryCursor += nBoundaryFaces
            return faceField

    
        mesh.owner = padFaceField('owner')
        mesh.neighbour = padFaceField('neighbour')
        mesh.neighbour[self.nInternalFaces:nLocalInternalFaces] -= nLocalBoundaryFaces
        mesh.neighbour[mesh.nInternalFaces:remoteGhostStartFace] += nLocalRemoteBoundaryFaces
        # do mapping, vectorize? mostly not possible
        internalCursor = nLocalInternalFaces
        boundaryCursor = remoteGhostStartFace
        internalCellsCursor = self.nInternalCells
        boundaryCellsCursor = self.nCells
        for patchID in self.remotePatches:
            reverseInternalMapping = {v:(k + internalCellsCursor) for k,v in enumerate(remoteInternal['mapping'][patchID])}
            nInternalFaces = len(remoteInternal['owner'][patchID])
            for index in range(internalCursor, internalCursor + nInternalFaces):
                mesh.owner[index] = reverseInternalMapping[mesh.owner[index]]
                mesh.neighbour[index] = reverseInternalMapping[mesh.neighbour[index]]
            internalCursor += nInternalFaces
            internalCellsCursor += len(remoteInternal['mapping'][patchID])
            reverseBoundaryMapping = {v:(k + boundaryCellsCursor) for k,v in enumerate(remoteBoundary['mapping'][patchID])}
            nBoundaryFaces = len(remoteBoundary['owner'][patchID])
            for index in range(boundaryCursor, boundaryCursor + nBoundaryFaces):
                mesh.owner[index] = reverseInternalMapping[mesh.owner[index]]
                mesh.neighbour[index] = reverseBoundaryMapping[mesh.neighbour[index]]
            boundaryCursor += nBoundaryFaces
            boundaryCellsCursor += len(remoteBoundary['mapping'][patchID])
 

        mesh.areas = padFaceField('areas')
        mesh.normals = padFaceField('normals')
        #print sum(config.norm(mesh.normals[remoteGhostStartFace:], axis=1)), nRemoteBoundaryFaces
        mesh.weights = padFaceField('weights')
        mesh.sumOp = self.getSumOp(mesh, ghost=True)

        mesh.volumes = np.zeros((mesh.nInternalCells, 1), config.precision)
        mesh.volumes[:self.nInternalCells] = self.volumes
        internalCursor = self.nInternalCells
        for patchID in self.remotePatches:
            nInternalCells = self.boundary[patchID]['nFaces']
            mesh.volumes[internalCursor:internalCursor + nInternalCells] = remoteInternal['volumes'][patchID][:nInternalCells]
            internalCursor += nInternalCells

        mesh.origMesh = cls.copy(mesh, fields=True)
        mesh.makeShared()

        return mesh
    
    def makeShared(self):
        for attr in Mesh.constants:
            setattr(self, attr, T.shared(getattr(self, attr)))
        for attr in Mesh.fields:
            value = getattr(self, attr) 
            if attr == 'boundary': continue
            #if value.dtype == np.int32:
            #    value = value.astype(np.int64)
            if value.shape[1:] == (1,):
                setattr(self, attr, T.shared(value, broadcastable=config.broadcastPattern))
            else:
                setattr(self, attr, T.shared(value))
        for patchID in self.boundary:
            patch = self.boundary[patchID]
            patch['startFace'] = T.shared(patch['startFace'])
            patch['nFaces'] = T.shared(patch['nFaces'])
 
def removeCruft(content, keepHeader=False):
    # remove comments and newlines
    content = re.sub(re.compile('/\*.*\*/',re.DOTALL ) , '' , content)
    content = re.sub(re.compile('//.*\n' ) , '' , content)
    content = re.sub(re.compile('\n\n' ) , '\n' , content)
    # remove header
    if not keepHeader:
        content = re.sub(re.compile('FoamFile\n{(.*?)}\n', re.DOTALL), '', content)
    return content


def extractField(data, size, dimensions):
    if size == 0:
        return np.zeros((0,) + dimensions, config.precision)
    extractScalar = lambda x: re.findall('[0-9\.Ee\-]+', x)
    if dimensions == (3,):
        extractor = lambda y: list(map(extractScalar, re.findall('\(([0-9\.Ee\-\r\n\s\t]+)\)', y)))
    else:
        extractor = extractScalar
    nonUniform = re.search('nonuniform', data)
    data = re.search(re.compile('[A-Za-z<>\s\r\n]+(.*)', re.DOTALL), data).group(1)
    if nonUniform is not None:
        start = data.find('(') + 1
        end = data.rfind(')')
        if start == end:
            internalField = np.zeros((size, ) + dimensions)
        elif config.fileFormat == 'binary':
            internalField = np.array(np.fromstring(data[start:end], dtype=np.float64))
        else:
            internalField = np.array(np.array(extractor(data[start:end]), dtype=np.float64))
    else:
        internalField = np.array(np.tile(np.array(extractor(data)), (size, 1)), dtype=np.float64)
    internalField = internalField.reshape((size, ) + dimensions)
    return internalField.astype(config.precision)

def writeField(handle, field, dtype, initial):
    handle.write(initial + ' nonuniform List<'+ dtype +'>\n')
    handle.write('{0}\n('.format(len(field)))
    if config.fileFormat == 'binary':
        handle.write(ad.value(field.astype(np.float64)).tostring())
    else:
        handle.write('\n')
        for value in ad.value(field):
            if dtype == 'scalar':
                handle.write(str(value[0]) + '\n')
            else:
                handle.write('(' + ' '.join(np.char.mod('%f', value)) + ')\n')
    handle.write(')\n;\n')

