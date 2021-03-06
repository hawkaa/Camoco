import numpy as np
from scipy.misc import comb 

cdef extern from "math.h":
    bint isnan(double x)
    double sqrt(double x)

# input is a typed numpy memoryview (::1 means c contiguous array)
def pair_correlation(double[:, ::1] x):
    # Define a new memoryview on an empty gene X gene matrix
    cdef double[:, ::] res = np.empty((x.shape[0], x.shape[0]))
    cdef double u, v
    cdef int i, j, k, count
    cdef double du, dv, d, n, r
    cdef double sum_u, sum_v, sum_u2, sum_v2, sum_uv

    for i in range(x.shape[0]):
        for j in range(i, x.shape[0]):
            sum_u = sum_v = sum_u2 = sum_v2 = sum_uv = 0.0
            count = 0            
            for k in range(x.shape[1]):
                u = x[i, k]
                v = x[j, k]
                # skips if u or v are nans
                if u == u and v == v:
                    sum_u += u
                    sum_v += v
                    sum_u2 += u*u
                    sum_v2 += v*v
                    sum_uv += u*v
                    count += 1
            if count < 10:
                res[i, j] = res[j, i] = np.nan
                continue

            um = sum_u / count
            vm = sum_v / count
            n = sum_uv - sum_u * vm - sum_v * um + um * vm * count
            du = sqrt(sum_u2 - 2 * sum_u * um + um * um * count) 
            dv = sqrt(sum_v2 - 2 * sum_v * vm + vm * vm * count)
            r = 1 - n / (du * dv)
            res[i, j] = res[j, i] = r
    # Return the base of the memory view
    return res.base

def coex_index(long[:] ids, int mi):

    cdef long[::] indices = np.empty(comb(ids.shape[0],2,exact=True),dtype=np.long)
    cdef long count = 0
   
    for ix in range(ids.shape[0]):
        for jx in range(ix+1,ids.shape[0]):
            i = min(ids[ix],ids[jx])
            j = max(ids[ix],ids[jx])
            # Calculate what the index would be if it were a square matrix
            k = ((i * mi) + j) 
            # Calculate the number of cells in the lower diagonal
            ld = (((i+1)**2) - (i+1))/2
            # Calculate the number of items on diagonal
            d = i + 1
            indices[count] = k-ld-d
            count += 1
    return indices.base 


