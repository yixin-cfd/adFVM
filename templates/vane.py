import numpy as np

from adFVM import config
from adFVM.compat import norm, intersectPlane
from adFVM.density import RCF 
from adFVM import tensor

#primal = RCF('/home/talnikar/adFVM/cases/vane_optim/foam/laminar/3d_baseline/par-16/', objective='drag', objectiveDragInfo='pressure')
#primal = RCF('/master/home/talnikar/adFVM/cases/vane/les/', faceReconstructor='SecondOrder')#, timeIntegrator='euler')
#primal = RCF('/master/home/talnikar/foam/blade/les/')
#primal = RCF('/lustre/atlas/proj-shared/tur103/les/')

def dot(a, b):
    return ad.reshape(ad.sum(a*b, axis=1), (-1,1))

# heat transfer
def objectiveHeatTransfer(solver, mesh):
    U, T, p = tensor.CellTensor((3,)), tensor.CellTensor((1,)), tensor.CellTensor((1,))
    Ti = T.extract(mesh.owner)
    Tw = 300.
    dtdn = (Tw-Ti)/mesh.deltas
    k = solver.Cp*solver.mu(Tw)/solver.Pr
    ht = k*dtdn*mesh.areas
    w = mesh.areas*1.
    return tensor.TensorFunction('objective2', [U, T, p, mesh.areas, mesh.deltas, mesh.owner], [ht, w])

# pressure loss
def getPlane(solver):
    point = np.array([0.052641,-0.1,0.005])
    normal = np.array([1.,0.,0.])
    interCells, interArea = intersectPlane(solver.mesh, point, normal)
    return {'cells':interCells.astype(np.int32), 
            'areas': interArea, 
           }

def objectivePressureLoss(solver, mesh):
    ptin = 175158.
    normal = np.array([1.,0.,0.])
    U, T, p = tensor.CellTensor((3,)), tensor.CellTensor((1,)), tensor.CellTensor((1,))
    areas = tensor.Tensor((1,))
    cells = tensor.Tensor((1,), [tensor.IntegerScalar()])
    g = solver.gamma
    pi = p.extract(cells)
    Ti = T.extract(cells)
    Ui = U.extract(cells)
    rhoi = pi/(solver.Cv*Ti*(g- 1))
    ci = (g*pi/rhoi).sqrt()

    rhoUni = sum([rhoi*Ui[i]*normal[i] for i in range(0, 3)])
    Umagi = Ui.dot(Ui)
    Mi = Umagi.sqrt()/ci
    pti = pi*pow(1 + 0.5*(g-1)*Mi*Mi, g/(g-1))
    pl = (ptin-pti)*rhoUni*areas/ptin
    w = rhoUni*areas
    return tensor.TensorFunction('objective', [U, T, p, areas, cells], [pl, w])
    
#objective = objectiveHeatTransfer
objective = objectivePressureLoss

# CONVERT TO REDUCE/BCAST ops

objectiveString = """
scalar objective(const mat& U, const vec& T, const vec& p) {{
    integer nCells = rcf->objectivePLInfo["cells"].size()/sizeof(integer);
    integer* cells = (integer*) rcf->objectivePLInfo.at("cells").data();
    scalar* areas = (scalar*) rcf->objectivePLInfo.at("areas").data();
    const Mesh& mesh = *meshp;
    vec loss(nCells, true);
    vec weights(nCells, true);
    Function_objective(nCells, &U(0), &T(0), &p(0), \
        areas, cells, \
        &loss(0), &weights(0));
    scalar pl = loss.sum();
    scalar w = weights.sum();

    scalar ht = 0;
    scalar w2 = 0;
    vector<string> patches = {{"{0}", "{3}"}};
    for (string patchID : patches) {{
        integer startFace, nFaces;
        tie(startFace, nFaces) = mesh.boundaryFaces.at(patchID);
        vec heat(nFaces, true);
        vec weights(nFaces, true);
        for (integer f = startFace; f < startFace + nFaces; f++) {{
            if (patchID == "pressure") {{
                if ((mesh.faceCentres(f, 0) < 0.33757) || (mesh.faceCentres(f, 1) > 0.04692))
                continue;
            }}
            if (patchID == "suction") {{
                if ((mesh.faceCentres(f, 0) < 0.035241) || (mesh.faceCentres(f, 1) > 0.044337))
                continue;
            }}
            Function_objective2(1, &U(0), &T(0), &p(0), \
                &mesh.areas(f), &mesh.deltas(f), &mesh.owner(f), \
                &heat(f-startFace), &weights(f-startFace));
        }}
        ht += heat.sum();
        w2 += weights.sum();
    }}

    scalar val[4] = {{pl, w, ht, w2}};
    scalar gval[4];
    MPI_Allreduce(&val, &gval, 4, MPI_DOUBLE, MPI_SUM, MPI_COMM_WORLD);

    scalar a = {1};
    scalar b = {2};
    scalar obj = gval[0]/gval[1];
    scalar obj2 = gval[2]/gval[3];
    //cout << a*obj << " " << b*obj2 << endl;
    return a*obj + b*obj2;
}}
void objective_grad(const mat& U, const vec& T, const vec& p, mat& Ua, vec& Ta, vec& pa) {{
    const Mesh& mesh = *meshp;
    Mesh& meshAdj = *meshap;

    integer nCells = rcf->objectivePLInfo["cells"].size()/sizeof(integer);
    integer* cells = (integer*) rcf->objectivePLInfo.at("cells").data();
    scalar* areas = (scalar*) rcf->objectivePLInfo.at("areas").data();
    vec loss(nCells, true);
    vec weights(nCells, true);
    Function_objective(nCells, &U(0), &T(0), &p(0), \
        areas, cells, \
        &loss(0), &weights(0));
    scalar pl = loss.sum();
    scalar w = weights.sum();

    scalar ht = 0;
    scalar w2 = 0;
    vector<string> patches = {{"{0}", "{3}"}};
    for (string patchID : patches) {{
        integer startFace, nFaces;
        tie(startFace, nFaces) = mesh.boundaryFaces.at(patchID);
        vec heat(nFaces, true);
        vec weights(nFaces, true);
        for (integer f = startFace; f < startFace + nFaces; f++) {{
            if (patchID == "pressure") {{
                if ((mesh.faceCentres(f, 0) < 0.33757) || (mesh.faceCentres(f, 1) > 0.04692))
                continue;
            }}
            if (patchID == "suction") {{
                if ((mesh.faceCentres(f, 0) < 0.035241) || (mesh.faceCentres(f, 1) > 0.044337))
                continue;
            }}
            Function_objective2(1, &U(0), &T(0), &p(0), \
                &mesh.areas(f), &mesh.deltas(f), &mesh.owner(f), \
                &heat(f-startFace), &weights(f-startFace));
        }}
        ht += heat.sum();
        w2 += weights.sum();
    }}

    scalar val[4] = {{pl, w, ht, w2}};
    scalar gval[4];
    MPI_Allreduce(&val, &gval, 4, MPI_DOUBLE, MPI_SUM, MPI_COMM_WORLD);
    scalar gpl = gval[0];
    scalar gw = gval[1];
    scalar ght = gval[2];
    scalar gw2 = gval[3];
    scalar a = {1};
    scalar b = {2};

    vec lossa(nCells);
    vec weightsa(nCells);
    vec areasa(nCells);
    for (integer i = 0; i < nCells; i++) {{
        lossa(i) = a/gw;
        weightsa(i) = -a*gpl/(gw*gw);
    }}
    Function_objective_grad(nCells, &U(0), &T(0), &p(0), \
        areas, cells, \
        &lossa(0), &weightsa(0),\
        &Ua(0), &Ta(0), &pa(0), \
        &areasa(0), NULL);

    for (string patchID : patches) {{
        integer startFace, nFaces;
        tie(startFace, nFaces) = mesh.boundaryFaces.at(patchID);
        vec heata(nFaces);
        vec weightsa(nFaces);
        for (integer i = 0; i < nFaces; i++) {{
            heata(i) = b/gw;
            weightsa(i) = -b*ght/(gw*gw);
        }}
        for (integer f = startFace; f < startFace + nFaces; f++) {{
            if (patchID == "pressure") {{
                if ((mesh.faceCentres(f, 0) < 0.33757) || (mesh.faceCentres(f, 1) > 0.04692))
                continue;
            }}
            if (patchID == "suction") {{
                if ((mesh.faceCentres(f, 0) < 0.035241) || (mesh.faceCentres(f, 1) > 0.044337))
                continue;
            }}
            Function_objective2_grad(1, &U(0), &T(0), &p(0), \
                &mesh.areas(f), &mesh.deltas(f), &mesh.owner(f), \
                &heata(f-startFace), &weightsa(f-startFace),
                &Ua(0), &Ta(0), &pa(0), \
                &meshAdj.areas(f), &meshAdj.deltas(f), &meshAdj.owner(f));
        }}
    }}
}}
"""

primal = RCF('/home/talnikar/adFVM/cases/vane/laminar_gpu/', objective=[objectivePressureLoss, objectiveHeatTransfer], objectivePLInfo={}, \
             objectiveString = objectiveString)
primal.defaultConfig["objectivePLInfo"] = getPlane(primal)
a = 0.4
#a = 0.
k = primal.mu(300)*primal.Cp/primal.Pr
b = -0.71e-3/(120*k)/2000.
#b = 0.
primal.objectiveString = primal.objectiveString.format('pressure', a, b, 'suction')

def makePerturb(param, eps=1e-4):
    def perturbMesh(fields, mesh, t):
        if not hasattr(perturbMesh, 'perturbation'):
            ## do the perturbation based on param and eps
            #perturbMesh.perturbation = mesh.getPerturbation()
            points = np.zeros_like(mesh.points)
            #points[param] = eps
            points[:] = eps*mesh.points
            perturbMesh.perturbation = mesh.getPointsPerturbation(points)
        return perturbMesh.perturbation
    return perturbMesh
perturb = [makePerturb(1)]

parameters = 'mesh'

#def makePerturb(mid):
#    def perturb(fields, mesh, t):
#        G = 10*np.exp(-1e4*norm(mid-mesh.cellCentres[:mesh.nInternalCells], axis=1)**2)
#        #rho
#        rho = G
#        rhoU = np.zeros((mesh.nInternalCells, 3))
#        rhoU[:, 0] = G.flatten()*100
#        rhoE = G*2e5
#        return rho, rhoU, rhoE
#    return perturb
#perturb = [makePerturb(np.array([-0.02, 0.01, 0.005])),
#           makePerturb(np.array([-0.08, -0.01, 0.005]))]

#parameters = 'source'

#nSteps = 10
#writeInterval = 5
nSteps = 100
writeInterval = 100
#nSteps = 100000
#writeInterval = 5000
startTime = 3.0
dt = 1e-8

#adjParams = [1e-3, 'abarbanel', None]
