#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created in Dec 2020

@author: Guillaume Le Treut

"""
#==============================================================================
# libraries
#==============================================================================
import os
import copy
import re
import pickle as pkl
import numpy as np
try:
  import cupy as cp
except ImportError:
  cp = None
import pandas as pd
import datetime
from pathlib import Path
import shutil
import scipy.integrate
import scipy.stats as sst
import scipy.special as ssp
import matplotlib.pyplot as plt
import matplotlib.gridspec as mgs
import matplotlib.cm as cm
import matplotlib.colors as mco
import matplotlib.patches as mpatches
import matplotlib.colors as mco
import matplotlib.ticker as ticker
from matplotlib import animation

import imageio

#==============================================================================
# helpers methods
#==============================================================================
def get_binned(X, Y, edges):
    nbins = len(edges)-1
    digitized = np.digitize(X,edges)
    Y_subs = [None for n in range(nbins)]
    for i in range(1, nbins+1):
        Y_subs[i-1] = np.array(Y[digitized == i])

    return Y_subs

def get_array_module(a):
  """
  Return the module of an array a
  """
  if cp:
    return cp.get_array_module(a)
  else:
    return np

def geo_dist(M1, M2):
    """
    Input in degrees.
    M = (latitude, longitude)
    """
    p1, l1 = M1
    p2, l2 = M2
    pbar = 0.5*(p1+p2)

    dp = (p1-p2)*np.pi/180.
    dl = (l1-l2)*np.pi/180.
    pbar = 0.5*(p1+p2)*np.pi/180.
    return np.sqrt(dp**2+np.cos(pbar)*dl**2)

def unfold_positive(E, n=2**5, deg=12, nbins=2**6):
  """
  Based on: https://www.mathworks.com/matlabcentral/fileexchange/24122-unfoldingpositive

  INPUT:
    E: matrix of size nsamples x N
    n: integer (typical value 10 to 40)
    deg: integer (typical value 7 to 15)
    nbins: integer (typical value 40 to 80)
  OUTPUT:
    X: centers of bins
    Y: histogram of nearest-neighbor differences (unfolded)

  This code unfolds a positive sequence of 'N' eigenvalues for 'nsamples'
  matrix samples through polynomial fitting of the cumulative
  distribution.
  The fitting polynomial has degree 'deg'.
  The code takes as input a matrix E of size (nsamples x N) where row j
  contains the N positive eigenvalues of the j-th sample, and a number
  'n' of points between 0 and Ymax=max(max(E)).
  The cumulative distribution is computed over the 'n' points in the
  vector YR as the fraction of eigenvalues lying below YR(j) and stored
  in the vector CumDist.
  Then a polynomial fitting is performed over the cumulative density
  profile obtained in this way, and the resulting polynomial is then
  computed on all the entries of E (---> xiMatr1).
  The nearest-neighbor difference between the unfolded eigenvalues in xiMatr1 is then computed,
  and produces a normalized histogram Y with 'nbins' (number of bins) centered at X,
  ready to be plotted.

  """
  from numpy.polynomial import Polynomial

  nsamp = E.shape[0]
  N = E.shape[1]

  ymax = np.max(np.ravel(E))
  Yr = np.linspace(0,ymax,n)

  # compute cumulative distribution
  Z = (1./float(nsamp)/float(N)) * np.array([ np.sum(np.int_(E < y)) for y in Yr], dtype=np.float_)

  # theta = np.polyfit(Yr, Z, deg)
  # p = np.poly1d(theta)
  p = Polynomial.fit(Yr, Z, deg)
  FitDist = p(Yr)
  xiMatr1 = p(E)

  d = np.ravel(np.diff(xiMatr1, axis=1))
  return np.histogram(d,bins=nbins,density=True)

#==============================================================================
# modelling
#==============================================================================
########## fitting methods ##########
def fsigmoid(x, *params):
    a, b, c, d = list(params)
    return a / (1.0 + np.exp(d*(x-b))) + c

def fsigmoid_jac(x, *params):
    a, b, c, d = list(params)
    grad = np.zeros((len(params), len(x)))
    grad[0] = 1./(1.0 + np.exp(d*(x-b)))
    grad[1] = d*np.exp(d*(x-b)) * a /(1.0 + np.exp(d*(x-b)))**2
    grad[2] = np.ones(len(x))
    grad[3] = -(x-b)*np.exp(d*(x-b)) * a /(1.0 + np.exp(d*(x-b)))**2
    return grad.T

def framp(x, a, b, c):
    return c*np.logaddexp(0, a*(x-b))

def framp_jac(x, a, b, c):
    g = np.exp(-a*(x-b))
    J1 = (x-b)*c/(1.+g)
    J2 = -a*c/(1.+g)
    J3 = np.logaddexp(0, a*(x-b))
    return np.array([J1, J2, J3]).T

########## Utils ##########
def read_df(t, tfmt, store, path):
  """
  Read a matrix present in a `store` at a certain `path` with
  the appropriate formatting of the date `t`.
  """
  key = Path(path) / t.strftime(tfmt)
  df = store[str(key)]
  return df

def get_infectivity_matrix(F):
  """
  Return the infectivity matrix from the input flux matrix
  """
  N = F.shape[0]
  if (F.shape[1] != N):
    raise ValueError

  pvec = F.diagonal()
  pinv = np.zeros(N, dtype=np.float_)
  idx = pvec > 0.
  pinv[idx] = 1./pvec[idx]

  L = np.zeros((N,N), dtype=np.float_)
  L = F + F.T
  np.fill_diagonal(L, pvec)
  L = np.einsum('ij,i->ij', L, pinv)

  # symmetrize it
  L = 0.5*(L+L.T)

  return L

########## SIR integration ##########
def sir_X_to_SI(X, N):
  SI = X.reshape((2,N))
  return SI[0],SI[1]

def sir_X_to_SI_lattice_2d(X, n1, n2):
  S, I = X.reshape(2, 2**n1, 2**n2)
  return S, I

def sir_SI_to_X(S,I):
  xp = cp.get_array_module(S)
  return xp.ravel(xp.array([S,I]))

def func_sir_dX(t, X, B, g):
  """
  X: S, I
  B: localization matrix
  g: inverse recovery time
  """
  N = B.shape[0]
  S,I = sir_X_to_SI(X, N)

  dS = -np.einsum('i,ij,j->i', S, B, I)
  dI = -dS - g*I

  return sir_SI_to_X(dS,dI)

def jac(X, B, g):
  N = B.shape[0]
  SI = X.reshape((2,N))
  S = SI[0]
  I = SI[1]

  # derivative of f_S
  A1 = -  np.diag(np.einsum('ij,j->i', B, I))
  A2 = - np.einsum('ij,i->ij', B, S)
  A = np.concatenate([A1, A2], axis=1)

  # derivative of f_I
  B1 = -A1
  B2 = -A2 - g*np.eye(N)
  B = np.concatenate([B1, B2], axis=1)

  return np.concatenate([A,B], axis=0)

def get_sir_omega_X(X, P):
  """
  Compute the total fraction of T=I+R individuals from local fractions and local populations
  """
  N = len(P)
  return np.einsum('i,i', 1.-X.reshape(2, N)[0], P)/np.einsum('i->', P)

def get_sir_omega_SI(S, I, P):
  """
  Compute the total fraction of I+R individuals from local fractions and local populations
  """
  return get_sir_omega_X(sir_SI_to_X(S, I), P)

def compute_sir_X(X, dt, B, gamma, method_solver, t_eval=None):
  """
  Utility function to integrate the SIR dynamics by dt.
  """
  if t_eval is None:
    t_eval = [0., dt]

  sol = scipy.integrate.solve_ivp(func_sir_dX, y0=X, t_span=(0,dt), \
                                  t_eval=t_eval, vectorized=True, args=(B, gamma), \
                                  method=method_solver)

  # break conditions
  if not (sol.success):
    raise ValueError("integration failed!")

  Xnew = sol.y[:,-1:].T

  return Xnew

def get_epidemic_size(M, epsilon_i, gamma, itermax=1000, rtol_stop=1.0e-8):
  """
  Compute the epidemic size given an initial condition epsilon_i and a infectivity matrix M.
  epsilon_i represents the fraction of infected individuals at time t=0, in each community.
  """
  N = M.shape[0]
  if (M.shape[1] != N):
    raise ValueError
  if (len(epsilon_i) != N):
    raise ValueError

  Xnew = np.zeros(N, dtype=np.float_)
  for iter in range(itermax):
    X = Xnew.copy()
    B = 1. - (1.-epsilon_i)*np.exp(-X)
    Xnew = 1./gamma * np.einsum('ab,b', M, B)

    rtol = np.linalg.norm(X-Xnew)/(np.linalg.norm(X)+np.linalg.norm(Xnew))*2
#     print("rtol = {:.6e}".format(rtol))
    if (rtol < rtol_stop):
      break
    if (iter == itermax -1):
      #             raise ValueError("Algorithm didn't converge! rtol = {:.6e}".format(rtol))
      print("Algorithm didn't converge! rtol = {:.6e}".format(rtol))

  B = 1. - (1.-epsilon_i)*np.exp(-X)
  Omega = np.sum(B) / N
  return Omega

def get_target_scale(M, Ii, gamma, target=0.1, rtol_stop=1.0e-8, itermax=100):
  """
  Return the scale parameter to apply to the infectivity matrix in order to have an epidemic size equal to `target`.
  """
  from scipy.optimize import root_scalar

  # define function to zero
  func_root = lambda x: get_epidemic_size(x*M, Ii, gamma, itermax=itermax, rtol_stop=rtol_stop) - target

  # initial bracketing
  xlo = 1.0e-5
  flo = func_root(xlo)
  if flo > 0.:
    raise ValueError("Lower bound on scale not small enough!")
  xhi = xlo
  for k in range(10):
    fhi = func_root(xhi)
    if fhi > 0.:
      break
    else:
      xhi *= 10
  if fhi < 0.:
    raise ValueError("Problem in bracketing!")

  # root finding
  sol = root_scalar(func_root, bracket=(xlo, xhi), method='brentq', options={'maxiter': 100})
  return sol.root

def integrate_sir(Xi, times, scales, gamma, store, pathtoloc, tfmt='%Y-%m-%d', method_solver='DOP853', verbose=True):
  """
  Integrate the dynamics of the SIR starting from
  the initial condition (`Xi`, `times[0]`).
  The method assumes that in the `store` at the indicated `path`, there are entries
  in the format %Y-%m-%d that described the infectivity matrices
  for the times `times[:-1]`. The array `scales` contains the scales to apply to each infectivity matrix.

  OUTPUT:
    * Xs
    * ts

  For the output the dumping interval is 1 day.
  """
  # initializations
  nt = len(times)
  t = times[0]
  X = Xi[:]
  B = read_df(t, tfmt, store, pathtoloc).to_numpy()
  N = B.shape[0]

  if len(scales) != nt - 1:
    raise ValueError("`scales` must be of length {:d}".format(nt-1))

  ts = [t]
  Xs = [X]

  for i in range(1, nt):
    if verbose:
      print(f'Integrating day {t}')
    mykey = Path(pathtoloc) / t.strftime(tfmt)
    mykey = str(mykey)
    if mykey in store.keys():
      B = read_df(t, tfmt, store, pathtoloc).to_numpy()
    elif verbose:
      print("Infectivity matrix not updated!")
    tnew = times[i]
    dt = int((tnew - t).days)
    t_range = np.arange(dt+1)
    sol = scipy.integrate.solve_ivp(func_sir_dX, y0=X, t_span=(0,dt), \
                                    t_eval=t_range, vectorized=True, args=(B*scales[i-1], gamma), \
                                    method=method_solver)

    # break conditions
    if not (sol.success):
      raise ValueError("integration failed!")

    Xnew = sol.y[:,-1]

    # dump
    Xs += [x for x in sol.y[:, 1:].T]
    ts += [t + datetime.timedelta(days=int(x)) for x in t_range[1:]]

    # update
    t = tnew
    X = Xnew

  if verbose:
    print("Integration complete")

  SIs = np.array([sir_X_to_SI(x, N) for x in Xs])
  Ss = SIs[:,0]
  Is = SIs[:,1]

  return ts, Ss, Is

def fit_sir(times, T_real, gamma, population, store, pathtoloc, tfmt='%Y-%m-%d', method_solver='DOP853', verbose=True, \
            b_scale=1):
  """
  Fit the dynamics of the SIR starting from real data contained in `pathtocssegi`.
  The initial condition is taken from the real data.
  The method assumes that in the `store` at the indicated `path`, there are entries
  in the format %Y-%m-%d that described the infectivity matrices
  for the times `times[:-1]`.
  `populations` is the vector with the population per community.

  OUTPUT:
    * Xs
    * ts
    * scales

  For the output the dumping interval is one day.
  """

  # initializations
  nt = len(times)
  t = times[0]
  B = read_df(t, tfmt, store, pathtoloc).to_numpy()
  N = B.shape[0]
  Y_real = np.einsum('ta,a->t', T_real, population) / np.sum(population)

  X = np.zeros((2, N), dtype=np.float_)
  I = T_real[0]
  S = 1 - I
  X = sir_SI_to_X(S, I)

  y = get_sir_omega_X(X, population)

  ts = [t]
  Xs = [X.reshape(2,N)]
  Ys = [y]
  b_scales = []

  blo = 0.
  # print("nt = ", nt)

  for i in range(1, nt):
    if verbose:
      print(f'Integrating day {t}')
    mykey = Path(pathtoloc) / t.strftime(tfmt)
    mykey = str(mykey)
    if mykey in store.keys():
      B = read_df(t, tfmt, store, pathtoloc).to_numpy()
    elif verbose:
      print("Infectivity matrix not updated!")

    tnew = times[i]
    dt = int((tnew - t).days)
    ypred = Y_real[i]

    # root finding method
    func_root = lambda b: get_sir_omega_X(compute_sir_X(X, dt, b*B, gamma, method_solver), \
                                          population) - ypred

    # initial bracketing
    bhi = b_scale
    fscale = 3.
    for k in range(1,10):
      f = func_root(bhi)
      if f > 0:
        break
      else:
        bhi *= fscale
    if f < 0:
      raise ValueError("Problem in bracketing!")

    # find the root
    sol = scipy.optimize.root_scalar(func_root, bracket=(blo, bhi), method='brentq', \
                                      options={'maxiter': 100})
    if not (sol.converged):
      raise ValueError("root finding failed!")
    b_scale = sol.root

    # compute next state with optimal scale
    t_eval = np.arange(dt+1)
    Xnews = compute_sir_X(X, dt, b_scale*B, gamma, method_solver, t_eval=t_eval)
    Xnew = Xnews[-1]
    y = get_sir_omega_X(Xnew,population)
    print(f"b = {b_scale}, y = {y}, ypred = {ypred}, y-ypred = {y-ypred}")

    # dump
    # data.append(Xnew.reshape(2,N))
    Xs += [Xnew.reshape(2,N) for Xnew in Xnews]
    ts += [t + datetime.timedelta(days=int(dt)) for dt in t_eval[1:]]
    Ys.append(y)
    b_scales.append(b_scale)

    # update
    t = tnew
    X = Xnew

  b_scales.append(None)  # B has ndays-1 entries
  print("Fitting complete")

  # prepare export of results
  S = np.array([X[0] for X in Xs])
  I = np.array([X[1] for X in Xs])
  clusters = np.arange(N, dtype=np.uint)
  df_S = pd.DataFrame(data=S, index=ts, columns=clusters)
  df_I = pd.DataFrame(data=I, index=ts, columns=clusters)
  df_fit = pd.DataFrame(data=np.array([b_scales, Ys]).T, index=times, columns=["scale", "frac_infected_tot"])

  return df_S, df_I, df_fit

########## wave analysis methods ##########
def wave_ode_gamma_eq0(t, x, *f_args):
  """
  Right hand side of the wave equation ODE when gamma = 0
  """
  C = f_args[0]
  q, p = x

  return np.array([-q/(1.+p) + C*(1.-p), q,])

def wave_ode_gamma_neq0(t, X, *f_args):
  """
  Right hand side of the wave equation ODE when gamma > 0
  """
  C = f_args[0]
  D = f_args[1]
  CD = C*D
  x, y, z = X

  return np.array([-(1./(1.+y) + CD)*x  + C*(1+D*CD)*(z-y), x, CD*(z-y)])

def wave_front_get_ode_sol(C, D=0, p0=-0.99, tmin=0, tmax=1000, npts=1000, t_eval=None, eps=1.0e-3, method='BDF', x0_inf=1.0e-12, X0=None):
    from scipy.integrate import solve_ivp

    # first case: D = 0 (no recovery rate)
    if D == 0:
      func_ode = wave_ode_gamma_eq0

      def event_upperbound(t, x, *f_args):
          return np.abs(x[0])-x0_inf
          # return x[1]+p0

      event_upperbound.terminal=True

      if X0 is None:
        q0 = C*(1-p0**2)
        X0=np.array([q0,p0])
      args = [C]

      if t_eval is None:
        t_eval = np.linspace(tmin,tmax,npts)

      sol = solve_ivp(func_ode, t_span=[tmin,tmax], y0=X0, method=method, args=args, \
          events=event_upperbound, t_eval=t_eval)

      T = sol.t
      X = sol.y[0]
      Y = sol.y[1]
      return T, X, Y

    # second case: D > 0 (with recovery rate)
    else:
      func_ode = wave_ode_gamma_neq0

      def event_upperbound(t, X, *f_args):
#         return x[1]-1.0
        # return X[0]
        return np.abs(X[0]) - x0_inf

      event_upperbound.terminal=True

      def get_final_state(y0, tmax=10000, method=method):
        z0 = y0 + 2*eps # so that S+I+R=1
        x0 = 2*C*eps*(1.+y0)
        X0=np.array([x0,y0,z0])
        args = [C, D]

        sol = solve_ivp(func_ode, t_span=[0.,tmax], y0=X0, method=method, args=args, events=event_upperbound)
        return sol.y[1,-1]

      from scipy.optimize import root_scalar
      if X0 is None:
        func_min = lambda y: get_final_state(y) - 1.
        delta=0.01
        for it in range(6):
          ylo = -1+delta
          flo = func_min(ylo)
          if flo > 0:
            break
          else:
            delta /= 10
        if (flo < 0.):
          raise ValueError("flo < 0: change eps or tmax (most likely trajectory truncated early).")
        yhi = D-1
        fhi = func_min(yhi)
        if (fhi > 0.):
          raise ValueError("fhi > 0: change eps or tmax")

        rt = root_scalar(func_min, method='brentq', bracket=[ylo,yhi])
        y0 = rt.root

        z0 = y0 + 2*eps # so that S+I+R=1
        x0 = 2*C*eps*(1.+y0)
        X0=np.array([x0,y0,z0])

      args = [C, D]

      if t_eval is None:
        t_eval = np.linspace(tmin,tmax,npts)

      sol = solve_ivp(func_ode, t_span=[tmin, tmax], y0=X0, method=method, args=args, events=event_upperbound, t_eval=t_eval)

      T = sol.t
      X = sol.y[0]
      Y = sol.y[1]
      Z = sol.y[2]
      return T, X, Y, Z

########## lattice simulation methods ##########
def laplacian_discrete(X):
    """
    Compute the discrete laplacian of the matrix X such that:
      * there are dirichlet boundary conditions along the first axis
      * there are periodic boundary conditions along the second axis
    """
    xp = cp.get_array_module(X)

    # discrete laplacian with dirichlet boundary conditions
    Dx = xp.diff(X, append=0, axis=0) - xp.diff(X, prepend=0, axis=0)

    # discrete laplacian with periodic boundary conditions
    Dy = xp.diff(X, append=X[:,0].reshape(X.shape[0],1), axis=1) - xp.diff(X, prepend=X[:,-1].reshape(X.shape[0],1), axis=1)

    return Dx + Dy

def laplacian_discrete_slow(X):
    """
    Compute discrete Laplacian with Dirichlet boundary conditions along axis 0 and periodic boundary conditions along axis 1.
    Give same result as `laplacian_discrete`
    """
    xp = cp.get_array_module(X)
    N,M = X.shape

    Dx = xp.zeros((N,M))
    Dy = xp.zeros((N,M))

    Dx[1:-1] = X[:-2] + X[2:] - 2*X[1:-1]
    Dy[:, 1:-1] = X[:, :-2] + X[:, 2:] - 2*X[:, 1:-1]

    # Dirichlet boundary condition
    Dx[0] = X[1] - 2*X[0]
    Dx[-1] = X[-2] - 2*X[-1]

    # periodic boundary condition
    Dy[:,0] = X[:,-1] + X[:,1] - 2*X[:,0]
    Dy[:,-1] = X[:,-2] + X[:,0] - 2*X[:,-1]

    return Dx+Dy

def laplacian_discrete_conv(X, kernel_=np.array([[0, 1, 0],[1, -4, 1], [0, 1, 0]])):
    """
    Compute the discrete laplacian of the matrix X such that:
      * there are dirichlet boundary conditions along the first axis
      * there are periodic boundary conditions along the second axis

    The 9-pt stencil would be [[1, 2, 1], [2,-12,2], [1,2,1]]/4
    Give same result as `laplacian_discrete` when using the 5-pt stencil.
    """
    xp = cp.get_array_module(X)
    kernel = xp.array(kernel_)

    kp = np.array(kernel.shape) // 2
    xshape = np.array(X.shape)
    Y = xp.zeros(tuple(xshape + 2*kp), dtype=X.dtype)
    Y[kp[0]:kp[0]+xshape[0], kp[1]:kp[1]+xshape[1]] = X

    # boundary conditions
    ## Dirichlet along X
    Y[kp[0]-1] = 0.
    for i in range(1, kp[0]):
        Y[kp[0]-1-i] = - Y[kp[0]+i-1]
    Y[kp[0]+xshape[0]] = 0.
    for i in range(1, kp[0]):
        Y[kp[0]+xshape[0]+i] = - Y[kp[0]+xshape[0]-i]
    ## Periodic along Y
    Y[:, :kp[1]] = Y[:, xshape[1]:kp[1]+xshape[1]]
    Y[:, -kp[1]:] = Y[:, kp[1]:2*kp[1]]

    view_shape = tuple(np.subtract(Y.shape, kernel.shape) + 1) + kernel.shape
    strides = Y.strides + Y.strides

    sub_matrices = xp.lib.stride_tricks.as_strided(Y,view_shape,strides)

#     print(sub_matrices.shape, kernel.shape)
    return xp.einsum('ij,klij->kl',kernel,sub_matrices)

def lattice_2d_ode(t, X, alpha, beta, gamma, n1, n2):
    """
    function to integrate for the nearest neighbor SIR dynamics on a 2d lattice.
    """
    # extract S and I
    S, I = sir_X_to_SI_lattice_2d(cp.array(X), n1, n2)

    kernel = np.array([[1,2,1],[2,-12,2],[1,2,1]], dtype=np.float_)/4.  #9-pt stencil for the Laplacian computation

    # compute U
    U = ((alpha+4*beta)*I + beta*laplacian_discrete_conv(I, kernel))

    dS = -S*U
    dI = +S*U - gamma*I

    dX = sir_SI_to_X(dS, dI)
    return dX.get()


def lattice_2d_event_upperbound(t, X, alpha, beta, gamma, n1, n2):
    S, I = sir_X_to_SI_lattice_2d(cp.array(X), n1, n2)
    xp = cp.get_array_module(S)
    S_tot = float(xp.mean(S))
    T_tot = 1. - S_tot
    return T_tot - 0.99

def lattice_2d_integrate_sir(S0, I0, alpha, beta, gamma, tmax=100., tdump=1., method='Radau'):
    """
    Integrate the SIR dynamics on a lattice.
    INPUT:
      * S0: initial array of susceptible individuals
      * I0: initial array of infected individuals
      * alpha: intracommunity infectivity rate
      * beta: intercommunity infectivity rate
      * gamma: recovery rate

    OUTPUT:
      * times: times at which the trajectory is sampled
      * Ss: array of shape T x 2^n1 x 2^n2 representing the trajectory of susceptible individuals
      * Is: array of shape T x 2^n1 x 2^n2 representing the trajectory of infected individuals
    """
    from scipy.integrate import solve_ivp
    # determine n1 and n2
    N,M = S0.shape
    n1 = int(np.log2(N))
    if (N != 2**n1):
      raise ValueError("'N' must be a power of 2")
    n2 = int(np.log2(M))
    if (M != 2**n2):
      raise ValueError("'M' must be a power of 2")

   # initial condition
    args = [alpha, beta, gamma, n1, n2]
    X0 = sir_SI_to_X(S0, I0).copy().get()
    event_upperbound = lattice_2d_event_upperbound
    event_upperbound.terminal = True

    # integration
    sol = solve_ivp(lattice_2d_ode, t_span=[0.,tmax], y0=X0, method=method, \
                    args=args, t_eval=np.linspace(0,tmax,int(tmax/tdump) + 1), \
                    events=event_upperbound)

    times = sol.t
    Xs = np.array([sir_X_to_SI_lattice_2d(x, n1, n2) for x in sol.y.T])
    Ss = Xs[:,0]
    Is = Xs[:,1]
    return times, Ss, Is

def lattice_2d_ramp_fit(W, times, wmax, nfit=1000, maxfev=1000):
    """
    Fit the input function (times, W) to a ramp function.
    """
    from scipy.optimize import curve_fit

    idx = W<wmax
    Wfit = W[idx]
    npts = len(Wfit)
    ifit = max(1, int(float(npts)/nfit))
    Yfit = Wfit[::ifit]
    nfit = len(Yfit)
    Xfit = np.arange(nfit)

    a = 1.
    c = (Yfit[-1]-Yfit[0])/(Xfit[-1]-Xfit[0])
    b = Yfit[0] - c*np.log(2.)
    P0 = [a,b,c]
    P = curve_fit(framp, Xfit, Yfit, \
              p0=P0, jac=framp_jac, \
             maxfev=maxfev)[0]

    dt = np.diff(times)[0]
    t_inter = dt*ifit
    return P[0] / t_inter, P[1]*t_inter, P[2]

def lattice_2d_get_velocity(W, times, wmax, maxfev=1000):
    a, b, c = lattice_2d_ramp_fit(W, times, wmax=wmax, maxfev=maxfev)
    return a*c

def lattice_2d_get_velocity_theoretical(beta, gamma, alpha, S_ss=1.):
    """
    Return the theoretical lower bound for the wave velocity
    INPUT:
      * beta: intercommunity infectivity rate
      * gamma: recovery rate
      * alpha: intracommunity infectivity rate
      * S_ss: susceptible fraction right to the wave (before being hit).
    """
    a = 4 + alpha/beta
    return 2*beta*S_ss * np.sqrt(a - gamma/(beta*S_ss))

def lattice_2d_rescale_wave_profile(kfit, X, dT, Z_C, Y_C, v, dx=1.):
    """
    Fit the wave profile (X, dT) to the ODE solution (X_C, dT_C)
    """
    # recenter the profile around 0
    k0 = np.argmax(dT)
    x0 = X[k0]
    Z = kfit*(X.copy()-x0)

    # retain a window corresponding to the input ODE solution
    zlo = max(np.min(Z_C), np.min(Z))
    zhi = min(np.max(Z_C), np.max(Z))
    idx = (Z >= zlo) & (Z <= zhi)
    Z = Z[idx]
    Y = dT.copy()[idx]
    if (len(Z) > len(Z_C)):
        raise ValueError("Increase resolution of ODE solution!")

    # rescale Y
    Y /= (v*kfit/2.)

    return Z, Y

def lattice_2d_func_fit_wave_sol(kfit, X, G, X_C, dT_C, v, dx=1.):
    """
    Get the LSQ
    """
    X, G, X_binned, F = get_fit_wave_sol(kfit, X, G, X_C, dT_C, dx)

    n = len(X)
    return np.sum((F-G/(v*kfit/2.))**2)/n

#==============================================================================
# plot methods
#==============================================================================
def show_image(mat_, downscale=None, log=False, mpl=False, vmin=None, vmax=None, fileout=None, dpi=72, interpolation='none', method='sum'):
    mat = np.copy(mat_)
    N = mat.shape[0]
    if downscale:
        NK = N // downscale
        if method == 'sum':
          mat = mat[:NK*downscale, :NK*downscale].reshape(NK, downscale, NK, downscale).sum(axis=(1, 3))
        elif method == 'max':
          mat = mat[:NK*downscale, :NK*downscale].reshape(NK, downscale, NK, downscale).max(axis=(1, 3))
        elif method == 'maxmin':
          mat1 = mat[:NK*downscale, :NK*downscale].reshape(NK, downscale, NK, downscale).max(axis=(1, 3))
          mat12 = np.abs(mat1)
          mat2 = mat[:NK*downscale, :NK*downscale].reshape(NK, downscale, NK, downscale).min(axis=(1, 3))
          mat22 = np.abs(mat2)
          theta = np.int_(mat22 > mat12)
          mat = (1.-theta)*mat1 + theta*mat2

        else:
          raise ValueError("Method not implemented!")

    if not mpl:
        if log:
            mat = np.log(mat)

        fig = px.imshow(mat)
        return fig
        # fig.show()

    else:
        fig = plt.figure()
        ax = fig.gca()
        if log:
            img = ax.imshow(mat, norm=mco.LogNorm(vmin=vmin, vmax=vmax), extent=[0,N-1,N-1,0], origin='upper', interpolation=interpolation)
        else:
            img = ax.imshow(mat, origin='upper', interpolation=interpolation)

        plt.colorbar(img)
        if fileout:
          fig.savefig(fileout, dpi=dpi, bbox_inches='tight', pad_inches=0)
          print("Written file {:s}".format(str(fileout)))
          fig.clf()
          plt.close('all')
        else:
          return fig
          # plt.show()

def plot_omega_profile(Omegas, times, labels=None, colors=None, styles=None, fileout=Path('./animation.gif'), tpdir=Path('.'), \
                       dpi=150, lw=0.5, ms=2, idump=10, \
                       ymin=None, ymax=None, figsize=(4,3), fps=5, \
                       log=True, xlabel='community', ylabel="$\Omega_a$", lgd_ncol=2, deletetp=True, exts=['.png'], \
                       tfmt = "%Y-%m-%d"):
  """
  Save an animated image series (GIF) or movie (MP4), depending on the extension provided,
  representing the dynamics of local epidemic sizes
  See this tutorial on how to make animated movies:
    https://matplotlib.org/stable/api/animation_api.html
  INPUT:
    * Omegas: list of table containing omegas (indices t,a)
    * times: list of times (indices t)
  """
  # tp dir
  if not tpdir.is_dir():
    tpdir.mkdir(exist_ok=True)
  for ext in exts:
    for f in tpdir.glob('*' + ext): f.unlink()

  # parameters
  nseries = Omegas.shape[0]
  nt = len(times)
  if (Omegas.shape[1] != nt):
    raise ValueError("Omegas must have same second dimension as times!")
  N = Omegas.shape[2]

  haslabels=True
  if labels is None:
    haslabels=False
    labels = [None]*nseries

  if colors is None:
    colors = [None]*nseries

  if styles is None:
    styles = [None]*nseries
  for k in range(nseries):
    if styles[k] is None:
      styles[k] = 'o'

  num = int(np.ceil(np.log10(nt)))
  if float(nt) == float(10**num):
    num += 1
  # fmt = "{" + ":0{:d}".format(num) + "}"

  # determine minimum and maximum
  idx = Omegas[:,0,:] > 0.
  if ymin is None:
    ymin = 10**(np.floor(np.log10(np.min(Omegas[:,0,:][idx]))))    # closest power of 10
  if ymax is None:
    ymax = 10**(np.ceil(np.log10(np.max(Omegas))))    # closest power of 10
  print("ymin = {:.2e}".format(ymin), "ymax = {:.2e}".format(ymax))

  if not ".png" in exts:
    raise ValueError("PNG format must be given")

  # community index
  X = np.arange(N, dtype=np.uint)

  # prepare figure
  filenames=[]
  for i in range(nt):
    ## update time and Omega
    t = times[i]

    ## create figure
    fig = plt.figure(figsize=figsize, dpi=dpi)
    ax = fig.gca()

    date = t.strftime('%Y-%m-%d')
    title = "{:s}".format(date)
    ax.set_title(title, fontsize="large")

    for k in range(nseries):
      Y = Omegas[k, i]
      color=colors[k]
      label=labels[k]
      style=styles[k]
      ax.plot(X, Y, style, lw=lw, mew=0, ms=ms, color=color, label=label)

    ax.set_xlim(X[0], X[-1])
    ax.set_ylim(ymin,ymax)
    ax.set_xlabel(xlabel, fontsize='medium')
    ax.set_ylabel(ylabel, fontsize='large')
    if log:
      ax.set_yscale('log')
    ax.spines['right'].set_visible(False)
    ax.spines['top'].set_visible(False)
    ax.tick_params(length=4)

    if haslabels:
      ax.legend(loc='lower left', fontsize='medium', frameon=False, ncol=lgd_ncol)

    fname = str(tpdir / t.strftime(tfmt))
    # fname = str(tpdir / fmt.format(i))
    for ext in exts:
      fpath = fname + ext
      fig.savefig(fpath, dpi=dpi, bbox_inches='tight', pad_inches=0)
    fpath = fname + ".png"
    filenames.append(fpath)

    if (i %idump == 0):
      print(f"Written file {fpath}.")

    fig.clf()
    plt.close('all')

  # write movie
  imageio.mimsave(fileout, [imageio.imread(f) for f in filenames], fps=fps)
  print(f"Written file {fileout}.")

  # clean tpdir
  if deletetp:
    shutil.rmtree(tpdir)

  return

def plot_omega_map(Omega, times, XY, fileout=Path('./animation.gif'), tpdir=Path('.'), dpi=150, \
                   vmin=None, vmax=None, figsize=(4,3), nframes=None, fps=5, \
                   cmap=cm.magma_r, idump=1, tfmt = "%Y-%m-%d", ymin=None, ymax=None, \
                   clabel='$\Omega$', deletetp=True, exts=['.png'], \
                   circle_size=0.4, lw=0.1, edges=[], edge_width=0.5):
  """
  Save an animated image series (GIF) or movie (MP4), depending on the extension provided,
  representing the dynamics of local epidemic sizes

  INPUT:
    * df_tolls: list of dataframes
    * XY: 2xN array giving the coordinates of the N communities.
    *
  """
  from matplotlib.path import Path
  # tp dir
  if not tpdir.is_dir():
    tpdir.mkdir(exist_ok=True)
  for ext in exts:
    for f in tpdir.glob('*' + ext): f.unlink()

  # parameters
  nt = len(times)
  if (Omega.shape[0] != nt):
    raise ValueError("Omega must have same second dimension as times!")
  N = Omega.shape[1]

  num = int(np.ceil(np.log10(nt)))
  if float(nt) == float(10**num):
    num += 1
  fmt = "{" + ":0{:d}".format(num) + "}"

  # color scale
  # determine minimum and maximum
  idx = Omega[0,:] > 0.
  if vmin is None:
    vmin = 10**(np.floor(np.log10(np.min(Omega[0,:][idx]))))    # closest power of 10
  if vmax is None:
    vmax = 10**(np.ceil(np.log10(np.max(Omega))))    # closest power of 10
  print("vmin = {:.2e}".format(vmin), "vmax = {:.2e}".format(vmax))
  norm = mco.LogNorm(vmin=vmin, vmax=vmax)

  # clusters
  X, Y = XY
  xmin = np.min(X)
  xmax = np.max(X)
  if ymin is None:
    ymin = np.min(Y)
  if ymax is None:
    ymax = np.max(Y)

  # prepare figure
  filenames=[]
  for i in range(nt):
    if (i %idump != 0):
      continue
    ## update time and Omega
    t = times[i]

    ## create figure
    fig = plt.figure(figsize=figsize, dpi=dpi)
    ax = fig.gca()

    date = t.strftime('%Y-%m-%d')
    title = "{:s}".format(date)
    ax.set_title(title, fontsize="large")

    # draw edges
    if len(edges) > 0:
      for a1,a2 in edges:
        x1 = X[a1]
        y1 = Y[a1]
        x2 = X[a2]
        y2 = Y[a2]
        # ax.plot([x1,x2], [y1,y2], 'k-', lw=edge_width)
        verts = [ (x1, y1), (x2, y2)]
        codes = [Path.MOVETO, Path.LINETO]
        path = Path(verts, codes)
        patch = mpatches.PathPatch(path, facecolor='none', edgecolor='k', lw=edge_width)
        res = ax.add_patch(patch)

    # draw spheres
    Ns = np.arange(N)
    idx = np.argsort(Omega[i])
    # for a in range(N):
    for a in Ns[idx]:
      x = X[a]
      y = Y[a]
      val = Omega[i,a]
      if (val < vmin):
        color = [1.,1.,1.,1.]
      elif (val > vmax):
        color = [0.,0.,0.,1.]
      else:
        color = cmap(norm(val))
      circle = plt.Circle((x,y), circle_size, color=color, alpha=1, lw=lw, ec='black')
      res = ax.add_patch(circle)

    # formatting
    for lab in 'left', 'right', 'bottom', 'top':
      ax.spines[lab].set_visible(False)
    ax.tick_params(bottom=False, left=False, labelbottom=False, labelleft=False)
    cax = fig.add_axes(rect=[0.98,0.1,0.02,0.7])
    plt.colorbar(cm.ScalarMappable(norm=norm, cmap=cmap), cax=cax, label=clabel, extendfrac='auto')
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ax.set_aspect('equal')

    # write figure
    fname = str(tpdir / t.strftime(tfmt))
    for ext in exts:
      fpath = fname + ext
      fig.savefig(fpath, dpi=dpi, bbox_inches='tight', pad_inches=0)
    fpath = fname + ".png"
    filenames.append(fpath)

    print(f"Written file {fpath}.")

    fig.clf()
    plt.close('all')

  # write movie
  imageio.mimsave(fileout, [imageio.imread(f) for f in filenames], fps=fps)
  print(f"Written file {fileout}.")

  # clean tpdir
  if deletetp:
    shutil.rmtree(tpdir)

  return
