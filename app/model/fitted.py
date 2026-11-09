"""Regularized, time-weighted opponent-adjusted Poisson + fitted low-score correction.

Independent implementation of the statistical approach used by penaltyblog / football-mle.
Coordinate Newton fitting needs no numerical runtime dependency. Rho is fitted conditional
on the Poisson rates (two-stage fitting, NOT joint Dixon-Coles maximum likelihood).
"""
import math
from . import predict as P
from .ratings import played_before


class FittedGoals:
    def __init__(self, history, at, half_life=200, penalty=3.0):
        rows = [m for m in history if played_before(m, at)
                and all(type(m.get(k)) is int and m[k]>=0 for k in ('home_goals','away_goals'))]
        self.teams = sorted({m[s] for m in rows for s in ('home', 'away')})
        self.index = {t: i for i, t in enumerate(self.teams)}
        n = len(self.teams)
        self.n = len(rows)
        self.params = [0.0] * (2*n+2)
        self.rho = 0.0
        self.converged = False
        self.counts = {t: 0 for t in self.teams}
        if not rows:
            return
        obs = []
        weighted = []
        for m in rows:
            h, a = self.index[m['home']], self.index[m['away']]
            w = 2 ** (-max(0, (at-m['ts']).total_seconds()/86400)/half_life)
            hg, ag = m['home_goals'], m['away_goals']
            obs.extend([((h,n+a,2*n),hg,w), ((a,n+h,2*n+1),ag,w)])
            weighted.append((h,a,hg,ag,w))
            self.counts[m['home']] += 1
            self.counts[m['away']] += 1
        memberships = [[] for _ in self.params]
        for i,(indices,_,_) in enumerate(obs):
            for j in indices: memberships[j].append(i)
        eta = [0.0]*len(obs)
        # Convex penalized Poisson objective, exact cyclic coordinate Newton steps.
        previous_objective = math.inf
        for iteration in range(400):
            change = 0.0
            for j, indices in enumerate(memberships):
                ridge = penalty if j < 2*n else .01
                gradient, curvature = ridge*self.params[j], ridge
                for i in indices:
                    _, y, w = obs[i]
                    expected = math.exp(eta[i])
                    gradient += w*(expected-y)
                    curvature += w*expected
                step = max(-.5,min(.5,gradient/curvature))
                self.params[j] -= step
                for i in indices: eta[i] -= step
                change = max(change,abs(step))
            # Center the attack/defence effects; transfer their means to both intercepts.
            # This leaves every fitted rate unchanged and removes weak scale directions.
            for start in (0,n):
                center=sum(self.params[start:start+n])/n
                for j in range(start,start+n):self.params[j]-=center
                self.params[2*n]+=center
                self.params[2*n+1]+=center
            objective=sum(w*(math.exp(eta[i])-y*eta[i]) for i,(_,y,w) in enumerate(obs))
            objective+=penalty/2*sum(v*v for v in self.params[:2*n])
            objective+=.005*sum(v*v for v in self.params[2*n:])
            stable=abs(previous_objective-objective)<1e-9*(1+abs(objective))
            previous_objective=objective
            if change < 1e-5 or stable:
                self.converged = True
                break
        self.iterations = iteration+1
        # Restrict rho so all four corrected score probabilities remain positive.
        rates = [(math.exp(self.params[h]+self.params[n+a]+self.params[2*n]),
                  math.exp(self.params[a]+self.params[n+h]+self.params[2*n+1]),hg,ag,w)
                 for h,a,hg,ag,w in weighted]
        def objective(rho):
            value = -10*rho*rho
            for lh,la,hg,ag,w in rates:
                if min(1-lh*la*rho,1+lh*rho,1+la*rho,1-rho) <= 0: return -math.inf
                tau = (1-lh*la*rho if hg==ag==0 else 1+lh*rho if hg==0 and ag==1
                       else 1+la*rho if hg==1 and ag==0 else 1-rho if hg==ag==1 else 1)
                value += w*math.log(tau)
            return value
        self.rho = max((i/100 for i in range(-20,21)),key=objective)

    def available(self, home, away):
        return self.converged and self.n >= 120 and min(self.counts.get(home,0),self.counts.get(away,0)) >= 8

    def rates(self, home, away):
        n = len(self.teams)
        h,a = self.index[home],self.index[away]
        return tuple(max(.15,min(5.0,math.exp(v))) for v in (
            self.params[h]+self.params[n+a]+self.params[2*n],
            self.params[a]+self.params[n+h]+self.params[2*n+1]))

    def matrix(self, home, away):
        lh,la = self.rates(home,away)
        rho = max(-.99/max(lh,la),min(self.rho,.99/(lh*la),.99))
        return P.score_matrix(lh,la,rho)
