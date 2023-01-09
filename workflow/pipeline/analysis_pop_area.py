"""This module was auto-generated by datajoint from an existing schema"""

import datajoint as dj
import numpy as np
import scipy
# import os.path
from bisect import bisect
import math
from math import *

schema = dj.Schema('lee_meso_analysis')

exp2 = dj.VirtualModule('exp2', 'arseny_s1alm_experiment2')
img = dj.VirtualModule('img', 'arseny_learning_imaging')
stimanal = dj.VirtualModule('stimanal', 'arseny_learning_photostim_anal')
lab = dj.VirtualModule('lab', 'map_lab')


def FetchChunked(relation, idx_name, val_name, chunk_size):
    idx = relation.fetch(idx_name, order_by=idx_name)
    num_elements = len(idx)
    num_chunks = (num_elements + (chunk_size - 1)) // chunk_size
    parts = []
    for i_chunk in range(num_chunks):
        i = i_chunk * chunk_size + 1
        # Don't need to manually check for the remainder; relation does it
        rel_part = relation & f"{idx_name} >= {i}" & f"{idx_name} < {i + chunk_size}"
        parts.append(np.asarray(rel_part.fetch(val_name, order_by=idx_name)))
    return np.concatenate(parts)


def InsertChunked(relation, data, chunk_size):
    num_elements = len(data)
    num_chunks = (num_elements + chunk_size - 1) // chunk_size
    for i_chunk in range(num_chunks):
        i = i_chunk * chunk_size
        relation.insert(data[i : min(i + chunk_size, num_elements)])


def MakeBins(F, bin_size):
    ceiled_bin_size = math.ceil(bin_size)
    if ceiled_bin_size == 0:
        return F
    num_bins = len(F) // ceiled_bin_size
    return [sum(F[i * ceiled_bin_size : (i + 1) * ceiled_bin_size]) / ceiled_bin_size for i in range(num_bins)]


def NormalizeF(F_binned, threshold, flag_zscore):
    if threshold > 0:
        F_zscored = scipy.stats.zscore(F_binned, 1)
        for i, fzs in enumerate(F_zscored):
            if fzs <= threshold:
                F_binned[i] = 0
    if flag_zscore: # zscoring the data
        return scipy.stats.zscore(F_binned, 1)
    else: # only centering the data
        return [f - fm for f, fm in zip(F_binned, np.mean(F_binned, 1))]


def FloatRange(start, stop, step):
    num_steps = int((stop - start) / step) + 1
    return [start + i * step for i in range(num_steps)]


@schema
class ROISVDArea(dj.Computed):
    definition = """
    -> exp2.SessionEpoch
    -> img.ROI
    -> lab.BrainArea
    threshold_for_event  : double                       # threshold in zscore, after binning. 0 means we don't threshold. 1 means we take only positive events exceeding 1 std, 2 means 2 std etc.
    time_bin             : double                       # time window used for binning the data. 0 means no binning
    ---
    roi_components       : longblob                     # contribution of the temporal components to the activity of each neurons; fetching this table for all neurons should give U in SVD of size (neurons x components) for the top num_comp components
    """

    @property
    def key_source(self):
        return (exp2.SessionEpoch & img.ROIdeltaF & lab.ROIBrainArea & stimanal.MiceIncluded) - exp2.SessionEpochSomatotopy

    def make(self, key):
    	# So far the code is only correct for threshold == 0
        thresholds_for_event = [0] # [0, 1, 2]

        rel_temp = img.Mesoscope & key
        if len(rel_temp) > 0:
            time_bin_vector = [0]
        else:
            time_bin_vector = [0.2, 0.5, 1]

        flag_zscore = 1
        threshold_variance_explained = 0.9
        num_components_save = 1000

        rel_data1 = (img.ROIdeltaF*img.ROIBrainArea & key) - img.ROIBad
        self2 = SVDAreaSingularValues
        self3 = SVDAreaTemporalComponents
        key.brain_area = 'MOp'
        for i, time_bin in enumerate(time_bin_vector):
            self.compute_SVD(self2, self3, key, rel_data1, flag_zscore, time_bin, thresholds_for_event, threshold_variance_explained, num_components_save)

    def compute_SVD(self, self2, self3, key, rel_data1, flag_zscore, time_bin, thresholds_for_event, threshold_variance_explained, num_components_save):
        rel_FOVEpoch = img.FOVEpoch & key
        rel_FOV = img.FOV & key
        if 'imaging_frame_rate' in rel_FOVEpoch.heading.secondary_attributes:
            imaging_frame_rate = rel_FOVEpoch.fetch1('imaging_frame_rate')
        else:
            imaging_frame_rate = rel_FOV.fetch1('imaging_frame_rate')

        # TODO: Use unique_roi_number or something esle to guarantee consistent order
        # (but unique_roi_number is not a primary key)

        if 'dff_trace' in rel_data1.heading.secondary_attributes:
            F = FetchChunked(rel_data1 & key, 'roi_number', 'dff_trace', 500)
        else:
            F = FetchChunked(rel_data1 & key, 'roi_number', 'spikes_trace', 500)

        F_binned = np.array([MakeBins(Fi.flatten(), time_bin * imaging_frame_rate) for Fi in F])

        for threshold in thresholds_for_event:
            F_normalized = NormalizeF(F_binned, threshold, flag_zscore)

            u, s, vh = np.linalg.svd(F_normalized, full_matrices=False)

            # in numpy, s is already just a vector; no need to take diag
            squared_s = s ** 2
            variance_explained = squared_s / sum(squared_s) # a feature of SVD. proportion of variance explained by each component
            cumulative_variance_explained = np.cumsum(variance_explained)
            num_comp = bisect(cumulative_variance_explained, threshold_variance_explained)
            u_limited = [ui[:num_comp] for ui in u]
            vt = vh[:num_components_save]

            # Populating POP.ROISVDPython
            key_ROIs = (rel_data1 & key).fetch('KEY', order_by='roi_number')
            for i in range(len(key_ROIs)):
                key_ROIs[i]['roi_components'] = u_limited[i]
                key_ROIs[i]['time_bin'] = time_bin
                key_ROIs[i]['threshold_for_event'] = threshold

            InsertChunked(self, key_ROIs, 1000)

            # Populating MESO.SVDAreaSingularValues and MESO.SVDAreaTemporalComponents
            svd_key = {**key, 'time_bin': time_bin, 'threshold_for_event': threshold}
            self2.insert1({**svd_key, 'singular_values': s}, allow_direct_insert=True)
            key_temporal = [{**svd_key, 'component_id': ic, 'temporal_component': vt[ic]}
                            for ic in range(num_components_save)]
            self3.insert(key_temporal, allow_direct_insert=True)


@schema
class SVDAreaSingularValues(dj.Computed):
    definition = """
    -> exp2.SessionEpoch
    threshold_for_event  : double                       # threshold in deltaf_overf
    time_bin             : double                       # time window used for binning the data. 0 means no binning
    ---
    singular_values      : longblob                     # singular values of each SVD temporal component, ordered from larges to smallest value
    """


@schema
class SVDAreaTemporalComponents(dj.Computed):
    definition = """
    -> exp2.SessionEpoch
    component_id         : int                          
    threshold_for_event  : double                       # threshold in deltaf_overf
    time_bin             : double                       # time window used for binning the data. 0 means no binning
    ---
    temporal_component   : longblob                     # temporal component after SVD (fetching this table for all components should give the Vtransopose matrix from SVD) of size (components x frames). Includes the top num_comp components
    """

