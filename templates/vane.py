from pyRCF import RCF 
import config
from config import ad
from compat import norm
import numpy as np

#primal = RCF('/home/talnikar/foam/blade/les-turb/')
#primal = RCF('/home/talnikar/foam/vane/laminar-lowRe/')#, timeIntegrator='euler')
primal = RCF('/home/talnikar/foam/vane/laminar/')#, timeIntegrator='euler')
#primal = RCF('/master/home/talnikar/foam/blade/les/')
#primal = RCF('/lustre/atlas/proj-shared/tur103/les/')

def dot(a, b):
    return ad.sum(a*b, axis=1, keepdims=True)

# heat transfer
def objectiveHeatTransfer(fields, mesh):
    rho, rhoU, rhoE = fields
    solver = rhoE.solver
    U, T, p = solver.primitive(rho, rhoU, rhoE)

    res = 0
    for patchID in ['suction', 'pressure']:
        startFace = mesh.boundary[patchID]['startFace']
        nFaces = mesh.boundary[patchID]['nFaces']
        endFace = startFace + nFaces
        internalIndices = mesh.owner[startFace:endFace]

        areas = mesh.areas[startFace:endFace]
        deltas = mesh.deltas[startFace:endFace]

        Ti = T.field[internalIndices] 
        Tw = 300
        dtdn = (Tw-Ti)/deltas
        k = solver.Cp*solver.mu(Tw)/solver.Pr
        hf = ad.sum(k*dtdn*areas)
        #dT = 120
        #A = ad.sum(areas)
        #h = hf/((nSteps + 1)*dT*A)
        res += hf
    return res

# pressure loss
def getPlane(solver):
    from compat import intersectPlane
    point = np.array([0.052641,-0.1,0.005])
    normal = np.array([1.,0.,0.])
    interCells, interArea = intersectPlane(solver.mesh, point, normal)
    #print interCells.shape, interArea.sum()
    solver.postpro.extend([(ad.ivector(), interCells), (ad.bcmatrix(), interArea)])
    return solver.postpro[-2][0], solver.postpro[-1][0], normal
    
def objectivePressureLoss(fields, mesh):
    #if not hasattr(objectivePressureLoss, interArea):
    #    objectivePressureLoss.cells, objectivePressureLoss.area = getPlane(primal)
    #cells, area = objectivePressureLoss.cells, objectivePressureLoss.area
    ptin = 171371.
    #actual ptin = 189718.8
    cells, area, normal = getPlane(primal)
    rho, rhoU, rhoE = fields
    solver = rhoE.solver
    g = solver.gamma
    U, T, p = solver.primitive(rho, rhoU, rhoE)
    pi, rhoi, Ui = p.field[cells], rho.field[cells], U.field[cells]
    rhoUi, ci = rhoi*Ui, ad.sqrt(g*pi/rhoi)
    rhoUni, Umagi = dot(rhoUi, normal), ad.sqrt(dot(Ui, Ui))
    Mi = Umagi/ci
    pti = pi*(1 + 0.5*(g-1)*Mi*Mi)**(g/(g-1))
    #res = ad.sum((ptin-pti)*rhoUni*area)/(ad.sum(rhoUni*area) + config.VSMALL)
    res = ad.sum((ptin-pti)*rhoUni*area)#/(ad.sum(rhoUni*area) + config.VSMALL)
    return res 

#objective = objectiveHeatTransfer
objective = objectivePressureLoss

def makePerturb(mid):
    def perturb(fields, mesh, t):
        G = 10*np.exp(-1e2*norm(mid-mesh.cellCentres[:mesh.nInternalCells], axis=1)**2)
        #rho
        rho = G
        rhoU = np.zeros((mesh.nInternalCells, 3))
        rhoU[:, 0] = G.flatten()*100
        rhoE = G*2e5
        return rho, rhoU, rhoE
    return perturb

perturb = [makePerturb(np.array([-0.08, 0.014, 0.005])),
           makePerturb(np.array([0.03, -0.03, 0.005]))]

#Steps = 20000
#riteInterval = 500
nSteps = 100000
writeInterval = 5000
startTime = 0.0
dt = 1e-8

