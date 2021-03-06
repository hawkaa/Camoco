#!/usr/bin/python3
import pyximport; pyximport.install()
import camoco.PCCUP as PCCUP

from camoco.Camoco import Camoco
from camoco.RefGen import RefGen
from camoco.Locus import Locus,Gene
from camoco.Expr import Expr
from camoco.Tools import memoize

from numpy import matrix,arcsinh,tanh
from collections import defaultdict,Counter
from itertools import chain
from subprocess import Popen, PIPE
from scipy.spatial.distance import squareform
from scipy.misc import comb
import statsmodels.api as sm
from statsmodels.sandbox.regression.predstd import wls_prediction_std

import pandas as pd
import igraph as ig
import numpy as np
import itertools
import matplotlib.pylab as plt

from scipy.stats import pearsonr

class COB(Expr):
    def __init__(self,name):
        super().__init__(name=name)
        self.hdf5 = self._hdf5(name)
        try:
            self.log('Loading coex table')
            self.coex = self.hdf5['coex']
        except KeyError as e:
            self.log("{} is empty ({})",name,e)
        try:
            self.log('Loading Global Degree')
            self.degree = self.hdf5['degree']
        except KeyError as e:
            self.log("{} is empty ({})",name,e)

    def __repr__(self):
        return '<COB: {}>'.format(self.name)

    def __str__(self):
        return self.__repr__()

    def summary(self):
        print( '''
            COB Dataset: {} - {} - {}
                Desc: {}
                RawType: {}
                TransformationLog: {}
                Num Genes: {}
                Num Accessions: {}
        '''.format(
                self.name, self.organism, self.build,
                self.description,
                self.rawtype,
                self._transformation_log(),
                self.num_genes(),
                self.num_accessions(),
        ))

    def coexpression(self,gene_a,gene_b):
        ''' 
            returns coexpression z-score between two genes 
        '''
        ids = np.array([self._expr_index[gene_a.id],self._expr_index[gene_b.id]])
        num_genes = self.num_genes()
        index = PCCUP.coex_index(ids,num_genes)[0]
        return self.coex.iloc[index]

    def subnetwork(self,gene_list=None,sig_only=True,min_distance=100000,fly_by_the_seat_of_your_pants=True):
        '''
            Input: a gene list (passing None gives you all genes)
            Output: a dataframe containing all edges EXCLUSIVELY between genes within list
        '''
        if gene_list is None:
            df = self.coex
        else:
            ids = np.array([self._expr_index[x.id] for x in gene_list])
            if fly_by_the_seat_of_your_pants:
                # filter out the Nones 
                ids = np.array(list(filter(None,ids)))
            num_genes = self.num_genes()
            indices = PCCUP.coex_index(ids,num_genes)
            df = self.coex.iloc[indices]
        if min_distance:
            df = df[df.distance >= min_distance]
        if sig_only:
            df = df[df.significant == 1]
        return df

    def density(self,gene_list,return_mean=True,min_distance=50000):
        ''' 
            calculates the denisty of the non-thresholded network amongst genes
            not within a certain distance of each other. This corrects for
            cis regulatory elements increasing noise in coexpression network 
        '''
        # filter for only genes within network
        edges = self.subnetwork(gene_list,min_distance=min_distance,sig_only=False)
        if len(edges) == 0:
            return np.nan
        if len(edges) == 1:
            return edges.score[0]
        if return_mean:
            return ((np.nanmean(edges.score)/((np.nanstd(edges.score))/np.sqrt(len(edges)))),
                    (np.nanmean(edges.score)/(1/np.sqrt(len(edges)))),
                    (np.nanmedian(edges.score)/((np.nanstd(edges.score))/np.sqrt(len(edges)))))
        else:
            return edges

    def plot_scores(self,filename=None,pcc=True,bins=50):
        ''' 
            Plot the histogram of PCCs.
        '''
        if filename is None:
            filename = self.name+'.png'
        plt.clf()
        # grab the scores only and put in a np array to save space (pandas DF was HUGE)
        scores = (self.coex.score.values + float(self._global('pcc_mean'))) * float(self._global('pcc_std'))
        if pcc:
            self.log('Transforming scores')
            # Transform Z-scores to pcc scores (inverse fisher transform)
            scores = tanh(scores)
        plt.hist(scores,bins=bins)
        plt.xlabel('PCC')
        plt.ylabel('Freq')
        plt.savefig(filename) 

    def to_dat(self,gene_list=None,filename=None,sig_only=False,min_distance=0):
        '''
            Outputs a .DAT file (see Sleipnir library)
        '''
        if filename is None:
            filename = self.name + '.dat'
        with open(filename, 'w') as OUT:
            self.log("Creating .dat file")
            self.subnetwork(
                gene_list,sig_only=sig_only,min_distance=min_distance
            )['score'].to_csv(OUT,sep='\t')
            self.log('Done')

    def mcl(self,gene_list=None,I=2.0,scheme=7,min_distance=100000,min_size=0,max_size=10e10):
        ''' 
            A *very* thin wrapper to the MCL program. The MCL program must
            be accessible by a subprocess (i.e. by the shell).
            Returns clusters (as list) as designated by MCL. 
            Input: a gene list
            Output: a list of lists of genes within each cluster
        '''
        # output dat to tmpfile
        tmp = self._tmpfile()
        self.to_dat(filename=tmp.name,gene_list=gene_list,min_distance=min_distance,sig_only=True)
        # build the mcl command 
        cmd = "mcl {} --abc -scheme {} -I {} -o -".format(tmp.name,scheme,I)
        self.log("running MCL: {}",cmd)
        try:
            p = Popen(cmd, stdout=PIPE, stderr=self.log_file, shell=True)
            self.log('waiting for MCL to finish...')
            sout = p.communicate()[0]
            p.wait()
            self.log('...Done')
            if p.returncode==0:
                # Filter out cluters who are smaller than the min size
                return list(filter(lambda x: len(x) > min_size and len(x) < max_size,
                    # Generate ids from the refgen
                    [ self.refgen.from_ids([gene.decode('utf-8') for gene in line.split()]) for line in sout.splitlines() ]
                ))
            else:
                self.log( "MCL failed: return code: {}".format(p.returncode))
        except FileNotFoundError as e:
            self.log('Could not find MCL in PATH. Make sure its installed and shell accessible as "mcl".')

    def local_degree(self,genes):
        '''
            Returns the local degree of a list of genes
        '''
        local_degree = pd.DataFrame(
            list(Counter(chain(*self.subnetwork(genes,sig_only=True).index.get_values())).items()),
            columns=['Gene','Degree']
        ).set_index('Gene')
        for gene in genes:
            if gene.id not in local_degree.index:
                local_degree.ix[gene.id] = 0
        return local_degree

    def global_degree(self,genes):
        '''
            Returns the global degree of a list of genes
        '''
        try:
            try:
                return self.degree.ix[[x.id for x in genes]].fillna(0)
            except TypeError as e:
                return self.degree.ix[genes.id].Degree
        except KeyError as e:
            return 0

    def locality(self,gene_list):
        '''
            Computes the merged local vs global degree table
        '''
        degree = self.local_degree(gene_list).merge(
            self.global_degree(gene_list),
            how='inner',
            left_index=True,
            right_index=True
        )
        degree.columns = ['local','global']
        # Add the regression lines
        degree['resid'] = sm.OLS(degree['global'],degree['local']).fit().resid
        return degree

    def plot_locality(self,gene_list,bootstraps=None,filename=None):
        plt.clf()
        if bootstraps is not None:
            for gene_list in bootstraps:
                degree = self.local_degree(gene_list).merge(
                    self.global_degree(gene_list),
                    left_index=True,
                    right_index=True
                )
                plt.scatter(degree.Degree_x,degree.Degree_y,alpha=0.05,color='red')
        degree = self.local_degree(gene_list).merge(
            self.global_degree(gene_list),
            left_index=True,
            right_index=True
        )
        plt.scatter(degree.Degree_x,degree.Degree_y,color='blue')
        plt.xlabel('Local Degree')
        plt.ylabel('Global Degree')
        if filename is None:
            filename = '{}_Locality.png'.format(self.name)
        plt.savefig(filename)
        return plt



    ''' ------------------------------------------------------------------------------------------
            Internal Methods
    '''
    def _calculate_coexpression(self,significance_thresh=3):
        ''' 
            Generates pairwise PCCs for gene expression profiles in self._expr.
            Also calculates pairwise gene distance.
        '''
        # Start off with a fresh set of genes we can pass to functions
        tbl = pd.DataFrame(
            list(itertools.combinations(self._expr.index.values,2)),
            columns=['gene_a','gene_b']
        )
        # Now add coexpression data
        self.log("Calculating Coexpression")
        # Calculate the PCCs
        pccs = 1-PCCUP.pair_correlation(np.ascontiguousarray(self._expr.as_matrix()))
        # Set the diagonal to zero (corrects for floating point errors)
        assert np.allclose(np.diagonal(pccs),1)
        np.fill_diagonal(pccs,0)
        # return the long form of the 
        pccs = squareform(pccs)
        assert len(pccs) == len(tbl)
        tbl['score'] = pccs
        # correlations of 1 dont transform well, they cause infinities
        tbl[tbl['score'] == 1].score = 0.99999999
        tbl[tbl['score'] == -1].score = -0.99999999
        # Perform fisher transform on PCCs
        tbl['score'] = np.arctanh(tbl['score'])
        # Sometimes, with certain datasets, the NaN mask overlap completely for the
        # two genes expression data making its PCC a nan. This affects the mean and std fro the gene.
        valid_scores = np.ma.masked_array(tbl['score'],np.isnan(tbl['score']))
        # Calculate Z Scores
        pcc_mean = valid_scores.mean()
        pcc_std = valid_scores.std()
        # Remember these so we can go back to PCCs
        self._global('pcc_mean',pcc_mean)
        self._global('pcc_std',pcc_std)
        tbl['score'] = (valid_scores-pcc_mean)/pcc_std
        # Assign significance
        tbl['significant'] = pd.Series(list(tbl['score'] >= significance_thresh),dtype='int_')
        self.log("Calculating Gene Distance")
        distances = self.refgen.pairwise_distance(gene_list=self.refgen.from_ids(self._expr.index))
        assert len(distances) == len(tbl)
        tbl['distance'] = distances
        # Reindex the table to match genes
        self.log('Indexing coex table')
        tbl.set_index(['gene_a','gene_b'],inplace=True)
        # put in the hdf5 store
        self._build_tables(tbl)
        self.log("Done")
        return self

    def _calculate_degree(self):
        try:
            self.log('Building Degree')
            self.hdf5['degree'] = pd.DataFrame(
                list(Counter(chain(*self.subnetwork(sig_only=True).index.get_values())).items()),
                columns=['Gene','Degree']
            ).set_index('Gene')
            self.hdf5.flush(fsync=True)
            self.degree = self.hdf5['degree']
        except Exception as e:
            self.log("Something bad happened:{}",e)
            raise

    def _build_tables(self,tbl):
        try:
            self.log("Building Database")
            ## HDF5 Store
            self.hdf5['coex'] = tbl
            self.log("Flushing Database")
            self.hdf5.flush(fsync=True)
            self.coex = self.hdf5['coex']
            self.log("Done")
        except Exception as e:
            self.log("Something bad happened:{}",e)
            raise

    def _coex_concordance(self,gene_a,gene_b,maxnan=10):
        '''
            This is a sanity method to ensure that the pcc calculated
            directly from the expr profiles matches the one stored in 
            the database
        '''
        expr_a = self.expr_profile(gene_a).values
        expr_b = self.expr_profile(gene_b).values
        mask = np.logical_and(np.isfinite(expr_a),np.isfinite(expr_b))
        if sum(mask) < maxnan:
            # too many nans to reliably calculate pcc 
            return np.nan
        r = pearsonr(expr_a[mask],expr_b[mask])[0]
        # fisher transform it
        z = np.arctanh(r-0.0000001)
        # standard normalize it
        z = (z - float(self._global('pcc_mean')))/float(self._global('pcc_std'))
        return z
 

    ''' ------------------------------------------------------------------------------------------
            Class Methods -- Factory Methods
    '''
    @classmethod
    def from_Expr(cls,expr):
        ''' 
            Create a COB instance from an camoco.Expr (Expression) instance.
            A COB inherits all the methods of a Expr instance and implements additional
            coexpression specific methods. This method accepts an already build Expr
            instance and then performs the additional computations needed to build a 
            full fledged COB instance.

            Parameters
            ----------
            expr : camoco.Expr
                
            Returns
            -------
            camoco.COB instance
            
        '''
        # The Expr object already exists, just get a handle on it
        self = expr
        self._calculate_coexpression()
        self._calculate_degree()
        return self

    @classmethod
    def from_DataFrame(cls,df,name,description,refgen,rawtype=None,**kwargs):
        '''
            The method will read the table in (as a pandas dataframe), 
            build the Expr object passing all keyword arguments in **kwargs
            to the classmethod Expr.from_DataFrame(...). See additional 
            **kwargs in COB.from_Expr(...)

            Parameters
            ----------
            df : pandas.DataFrame
                A Pandas dataframe containing the expression information.
                Assumes gene names are in the index while accessions (experiments)
                are stored in the columns.
            name : str
                Name of the dataset stored in camoco database
            description : str
                Short string describing the dataset
            refgen : camoco.RefGen
                A Camoco refgen object which describes the reference
                genome referred to by the genes in the dataset. This 
                is cross references during import so we can pull information
                about genes we are interested in during analysis. 
            rawtype : str (default: None)
                This is noted here to reinforce the impotance of the rawtype passed to 
                camoco.Expr.from_DataFrame. See docs there for more information.
            **kwargs : key value pairs
                additional parameters passed to subsequent methods. (see Expr.from_DataFrame)

        '''
        # Create a new Expr object from a data frame
        expr = super().from_DataFrame(
            df,name,description,refgen,rawtype,**kwargs
        )
        return cls.from_Expr(expr)

    @classmethod
    def from_table(cls,filename,name,description,refgen,rawtype=None,sep='\t',**kwargs):
        '''
            Build a COB Object from an FPKM or Micrarray CSV. This is a
            convenience method which handles reading in of tables.
            Files need to have gene names as the first column and 
            accession (i.e. experiment) names as the first row. All
            kwargs will be passed to COB.from_DataFrame(...). See
            docstring there for option descriptions.

            Parameters
            ----------
            filename : str (path)
                the path to the FPKM table in csv or tsv
            name : str
                Name of the dataset stored in camoco database
            description : str
                Short string describing the dataset
            refgen : camoco.RefGen
                A Camoco refgen object which describes the reference
                genome referred to by the genes in the dataset. This 
                is cross references during import so we can pull information
                about genes we are interested in during analysis. 
            rawtype : str (default: None)
                This is noted here to reinforce the impotance of the rawtype passed to 
                camoco.Expr.from_DataFrame. See docs there for more information.
            sep : str (default: \t)
                Specifies the delimiter of the file referenced by the filename parameter.
            **kwargs : key value pairs
                additional parameters passed to subsequent methods.

            Returns
            -------
                a COB object
        '''
        return cls.from_DataFrame(pd.read_table(filename,sep=sep),name,description,refgen,rawtype=rawtype,**kwargs)


    '''
        Unimplemented ---------------------------------------------------------------------------------
    '''

    def neighbors(self,gene_list,min_distance=None,sig_only=True):
        ''' Input : a list of COBGene Objects
            Output: Returns the neighbors for a list of genes as a DataFrame
            Columns returned: query_name, queryID, neighbor_name, neighbor, score
        '''
        pass

    def next_neighbors(self,gene_list):
        ''' returns a list containing the strongest connected neighbors '''
        pass

    def neighborhood(self,gene_list):
        ''' Input: A gene List
            Output: a Dataframe containing gene ids which have at least one edge
                    with another gene in the input list. Also returns global degree '''
        pass


    def lcc(self,gene_list,min_distance=None):
        ''' returns an igraph of the largest connected component in graph '''
        pass

    def seed(self, gene_list, limit=65): 
        ''' Input: given a set of nodes, add on the next X strongest connected nodes ''' 
        pass

    def graph(self,gene_list,min_distance=None):
        ''' Input: a gene list
            Output: a iGraph object '''
        pass

    def coordinates(self,gene_list,layout=None):
        ''' returns the static layout, you can change the stored layout by passing 
            in a new layout object. If no layout has been stored or a gene does not have
            coordinates, returns (0,0) for each mystery gene'''
        pass

    def cluster(self,raw=False):
        # Convert to tsv
        rawtype = 'raw' if raw else 'norm'
        filename  = '{}_{}.tsv'.format(self.name, rawtype)
        self.expr(raw=raw,zscore=True).to_csv(filename,sep='\t')
        try: 
            cmd = ['cluster', '-f', filename, '-g', '2', '-e', '2']
            self.log("Executing {}",' '.join(cmd))
            p = Popen(cmd, stderr=self.log_file, shell=True)
            self.log('Waiting for {} cluster...'.format(filename))
            p.wait()
        except FileNotFoundError as e:
            self.log('Could not find cluster command in PATH. Make sure its installed and shell accessible as "cluster".')

    def plot(self,gene_list,filename=None,width=3000,height=3000,layout=None,**kwargs):
        pass

    def compare_to_COB(self,COB_list,filename=None,gridsize=100,extent=[-10,10,-10,10]):
        ''' Compare the edge weights in this COB to another COB. Prints out edge weights to file'''
        for oCOB in COB_list:
            self.log("Comparing {} to {}",self.name,oCOB.name)
            filename = "{}_to_{}".format(self.name,oCOB.name)
            # Print out the edge comparisons for each common gene
            self.log("Printing out common gene edges")
            if not os.path.exists(filename+'.tsv'):
                with open(filename+'.tsv','w') as OUT:
                # merge the tables of edges on common genes
                    common_edges = self.coex.join(oCOB.coex, how='inner')

    def compare_to_dat(self,filename,sep="\t",score_cutoff=3):
        ''' Compare the number of genes with significant edges as well as degree with a DAT file '''
        pass

    def compare_degree(self,obj,score_cutoff=3):
        ''' Compares the degree of one COB to another '''
        pass
