
def scalarDissipation(pLF, pRF, TLF, TRF, ULF, URF, \
                rhoLF, rhoRF, rhoULF, rhoURF, rhoELF, rhoERF):
    # no TVD_dual for c in parallel
    cP = (gamma*pP/rhoP).sqrt
    c = CellField.getOrigField(cP)
    gradC = grad(central(cP, paddedMesh), ghost=True)
    cLF, cRF = TVD_dual(c, gradC)

    UnLF, UnRF = ULF.dotN(), URF.dotN()
    UnF = 0.5*(UnLF + UnRF)
    Z = Field('Z', ad.bcalloc(config.precision(0.), (mesh.nFaces, 1)), (1,))
    apF = Field.max(Field.max(UnLF + cLF, UnRF + cRF), Z)
    amF = Field.min(Field.min(UnLF - cLF, UnRF - cRF), Z)
    aF = Field.max(apF.abs(), amF.abs())

    rhoFlux = 0.5*(rhoLF*UnLF + rhoRF*UnRF) - 0.5*aF*(rhoRF-rhoLF)
    rhoUFlux = 0.5*(rhoULF*UnLF + rhoURF*UnRF + (pLF + pRF)*mesh.Normals) - 0.5*aF*(rhoURF-rhoULF)
    rhoEFlux = 0.5*((rhoELF + pLF)*UnLF + (rhoERF + pRF)*UnRF) - 0.5*aF*(rhoERF-rhoELF)

    return rhoFlux, rhoUFlux, rhoEFlux, aF, UnF


def eulerRoe(pLF, pRF, TLF, TRF, ULF, URF, \
                rhoLF, rhoRF, rhoULF, rhoURF, rhoELF, rhoERF):

    rhoUnLF, rhoUnRF = rhoLF*ULF.dotN(), rhoRF*URF.dotN()
    hLF = gamma*pLF/((gamma-1)*rhoLF) + 0.5*ULF.magSqr()
    hRF = gamma*pRF/((gamma-1)*rhoRF) + 0.5*URF.magSqr()

    rhoFlux = 0.5*(rhoUnLF + rhoUnRF)
    rhoUFlux = 0.5*(rhoUnLF*ULF + rhoUnRF*URF + (pLF + pRF)*mesh.Normals)
    rhoEFlux = 0.5*(rhoUnLF*hLF + rhoUnRF*hRF)

    sqrtRhoLF, sqrtRhoRF = rhoLF.sqrt(), rhoRF.sqrt()
    divRhoF = sqrtRhoLF + sqrtRhoRF
    UF = (ULF*sqrtRhoLF + URF*sqrtRhoRF)/divRhoF
    hF = (hLF*sqrtRhoLF + hRF*sqrtRhoRF)/divRhoF

    qF = 0.5*UF.magSqr()
    a2F = (gamma-1)*(hF-qF)
    # speed of sound for CFL
    aF = a2F.sqrt()
    # normal velocity for CFL
    UnF = UF.dotN()

    drhoF = rhoRF - rhoLF 
    drhoUF = rhoRF*URF - rhoLF*ULF
    drhoEF = (hRF*rhoRF-pRF)-(hLF*rhoLF-pLF)

    lam1, lam2, lam3 = UnF.abs(), (UnF + aF).abs(), (UnF - aF).abs()

    eps = 0.5*(rhoUnLF/rhoLF - rhoUnRF/rhoRF).abs()
    eps += 0.5*((gamma*pLF/rhoLF).sqrt() - (gamma*pRF/rhoRF).sqrt()).abs()

    lam1 = Field.switch(ad.lt(lam1.field, 2.*eps.field), 0.25*lam1*lam1/eps + eps, lam1)
    lam2 = Field.switch(ad.lt(lam2.field, 2.*eps.field), 0.25*lam2*lam2/eps + eps, lam2)
    lam3 = Field.switch(ad.lt(lam3.field, 2.*eps.field), 0.25*lam3*lam3/eps + eps, lam3)

    abv1 = 0.5*(lam2 + lam3)
    abv2 = 0.5*(lam2 - lam3)
    abv3 = abv1 - lam1
    abv4 = (gamma-1)*(qF*drhoF - UF.dot(drhoUF) + drhoEF)
    abv5 = UnF*drhoF - drhoUF.dotN()
    abv6 = abv3*abv4/a2F - abv2*abv5/aF
    abv7 = abv3*abv5 - abv2*abv4/aF

    rhoFlux -= 0.5*(lam1*drhoF + abv6)
    rhoUFlux -= 0.5*(lam1*drhoUF + UF*abv6 - abv7*mesh.Normals)
    rhoEFlux -= 0.5*(lam1*drhoEF + hF*abv6 - UnF*abv7)

    return rhoFlux, rhoUFlux, rhoEFlux, aF, UnF


def eulerHLLC(pLF, pRF, TLF, TRF, ULF, URF, \
                rhoLF, rhoRF, rhoULF, rhoURF, rhoELF, rhoERF):

    UnLF, UnRF = ULF.dotN(), URF.dotN()
    qLF, qRF = ULF.magSqr(), URF.magSqr()
    cLF, cRF = (gamma*pLF)/rhoLF, (gamma*pRF)/rhoRF
    hLF = gamma*pLF/((gamma-1)*rhoLF) + 0.5*qLF
    hRF = gamma*pRF/((gamma-1)*rhoRF) + 0.5*qRF
    eLF, eRF = hLF*rhoLF-pLF, hRF*rhoRF-pRF

    RrhoF = (rhoRF/rhoLF).sqrt()
    divRhoF = RrhoF + 1
    UF = (ULF + URF*RrhoF)/divRhoF
    # normal velocity for CFL
    UnF = UF.dotN()

    PrhoF = (cLF**2+0.5*(gamma-1)*qLF + (cRF**2+0.5*(gamma-1)*qRF)*RrhoF)/divRhoF
    cF = (PrhoF-0.5*(gamma-1)*UF.magSqr()).sqrt()
    # speed of sound for CFL
    aF = cF

    sLF = Field.min(UnF-cF, UnLF-cLF)
    sRF = Field.max(UnF+cF, UnRF+cRF)

    sMF = (pLF-pRF - rhoLF*UnLF*(sLF-UnLF) + rhoRF*UnRF*(sRF-UnRF)) \
          /(rhoRF*(sRF-UnRF)-rhoLF*(sLF-UnRF))
    pSF = rhoRF*(UnRF-sRF)*(UnRF-sMF) + pRF

    Frho1 = rhoLF*UnLF
    FrhoU1 = Frho1*ULF + pLF*mesh.Normals
    FrhoE1 = (eLF + pLF)*UnLF

    divsLF = sLF-sMF
    sUnLF = sLF-UnLF
    rhosLF = rhoLF*sUnLF/divsLF
    rhoUsLF = (rhoLF*ULF*sUnLF + (pSF-pLF)*mesh.Normals)/divsLF
    esLF = (sUnLF*eLF-pLF*UnLF+pSF*sMF)/divsLF

    Frho2 = rhosLF*sMF
    FrhoU2 = rhoUsLF*sMF + pSF*mesh.Normals
    FrhoE2 = (esLF + pSF)*sMF

    divsRF = sRF-sMF
    sUnRF = sRF-UnRF
    rhosRF = rhoRF*sUnRF/divsRF
    rhoUsRF = (rhoRF*URF*sUnRF + (pSF-pRF)*mesh.Normals)/divsRF
    esRF = (sUnRF*eRF-pRF*UnRF+pSF*sMF)/divsRF

    Frho3 = rhosRF*sMF
    FrhoU3 = rhoUsRF*sMF + pSF*mesh.Normals
    FrhoE3 = (esRF + pSF)*sMF

    Frho4 = rhoRF*UnRF
    FrhoU4 = Frho1*URF + pRF*mesh.Normals
    FrhoE4 = (eRF + pRF)*UnRF

    rhoFlux = Field.switch(ad.gt(sMF.field, 0.), \
              Field.switch(ad.gt(sLF.field, 0.), Frho1, Frho2), \
              Field.switch(ad.gt(sRF.field, 0.), Frho3, Frho4))
    
    rhoUFlux = Field.switch(ad.gt(sMF.field, 0.), \
              Field.switch(ad.gt(sLF.field, 0.), FrhoU1, FrhoU2), \
              Field.switch(ad.gt(sRF.field, 0.), FrhoU3, FrhoU4))

    rhoEFlux = Field.switch(ad.gt(sMF.field, 0.), \
              Field.switch(ad.gt(sLF.field, 0.), FrhoE1, FrhoE2), \
              Field.switch(ad.gt(sRF.field, 0.), FrhoE3, FrhoE4))

    return rhoFlux, rhoUFlux, rhoEFlux, aF, UnF
