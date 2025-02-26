# Includes some special purpose functions for "explaining" how various forms of matrix multiplication
# produce the results that they do.
# For example, you can obtain zero or near-zero results in a matmul operation for a number of reasons:
#  - everything on both inputs could be near-zero
#  - everything in just one input could be near-zero while the other had only large positive values
#  - both inputs could be a mixture of near-zeros and positive values that overlap in such a way that
#    you never get a combination of positive * positive.
#  - both inputs could contain large values, but some are negative, and they cancel out to produce a near-zero value.
#
# The functions here group the computations in such a way that makes it possibly to easily classify the results
# of a given computation to explain its output.
#
# General pattern and interpretation of results:
# Given:
#      value = <matmul-like-operation>(A, B)
# Then:
#      counts, sums = <matmul-like-operation>_classify(A, B)
#      terms = classify_terms(counts)
# Where:
#      counts, sums - both have shape: value_shape + (terms,)
#      terms        - list of 9 terms: ['PP', 'PZ', 'PN', 'ZP', 'ZZ', 'ZN', 'NP', 'NZ', 'NN']
# Such that:
#      value == np.sum(sums, axis=-1)
#
# Counts, sums, and terms together classify how the positive, near-zero, and negative components of the inputs
# contributed to each of the elements in 'value'. For example:
# Let:
#     value - have shape (32, 100)
# Then:
#     value[16,50] == np.sum(sums[16,50,:], axis=-1)     -- the original value can be derived from sums tensor
# And:
#     counts[16,50,0] / np.sum(counts[16,50,:])
#        -- gives fraction of value[16,50] that resulted from both A and B having positive values
#     sums[16,50,0]
#        -- gives the partial computation of values[16,50] resulting from just where A and B where both positive

# TODO I'm making a mistake by having filter_classifications() and group_classifications() doing sorting by default
# because then subsequent calls have to undo the sort.
# Need to change this around and only do the sorting at time of display.

import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
import conv_tools as ct


# TODO extend so that it can take a list of terms and do the right thing with them
def summarise(counts, sums=None, terms=None, *, mask=None, show_percentages=False, show_means=False, show_ranges=False):
    """
    Generates a concise summary text from the result of calling matmul_classify() or any of its variants.

    By default, lists the components ordered by largest count first.

    May be used in either form:
    > summarise(counts, sums)
    > summarise(counts, sums, terms)
    > summarise(xxx_classify(a,b))

    Example output:
    > PN: 13.0 = -5.6, PZ: 7.0 = 0.0, ZN: 1.0 = 0.0
    Meaning:
    - 13 instances of positive x negative, summing to -5.6
    - 7 instances of positive x near-zero, summing to 0.0
    - 1 instance of near-zero x negative, summing to 0.0

    Args:
        counts: the counts returned by matmul_classify() or one of its variants.
          Shape: value_shape + (terms,)
          Alternatively pass a tuple containing the counts and sums, and optionally
          the terms.
        sums: the sums returned by matmul_classify() or one of its variants.
          Shape: value_shape + (terms,)
        terms: terms returned by filter_classifications(), same shape as counts and sums.
          Shape: value_shape + (terms,)
          Must be included if filter_classifications() has been called.
        mask: bool with shape: value_shape.
        show_percentages: bool.
          Whether to show counts as a percentage of all counts being summarised
          (after masking), or as absolute values.
        show_means: bool.
          Whether to compute means from the counts and sums or show the raw sums.
        show_ranges: bool.
          Whether to show min/max range of counts and sums across the value axes,
          or just the sums/means.
          Cannot be combined with show_percentages or show_means.
    Returns:
        string description
    """
    # prepare
    # - split out counts-as-tensor
    # - standardize term order
    # - apply mask
    counts, sums, terms_list = standardize(counts, sums, terms, mask=mask, return_terms_as_list=True)

    # calculate summaries across each class
    counts_by_class = np.sum(counts, axis=tuple(range(counts.ndim - 1)))
    sums_by_class = np.sum(sums, axis=tuple(range(counts.ndim - 1)))
    min_counts_by_class = np.min(counts, axis=tuple(range(counts.ndim - 1)))
    max_counts_by_class = np.max(counts, axis=tuple(range(counts.ndim - 1)))
    min_sums_by_class = np.min(sums, axis=tuple(range(counts.ndim - 1)))
    max_sums_by_class = np.max(sums, axis=tuple(range(counts.ndim - 1)))

    # sort summary values by largest counts
    sort_order = np.argsort(counts_by_class)[::-1]
    counts_by_class = counts_by_class[sort_order]
    sums_by_class = sums_by_class[sort_order]
    min_counts_by_class = min_counts_by_class[sort_order]
    max_counts_by_class = max_counts_by_class[sort_order]
    min_sums_by_class = min_sums_by_class[sort_order]
    max_sums_by_class = max_sums_by_class[sort_order]
    terms_list = np.array(terms_list)[sort_order]

    # optional: convert sums to means
    # (make sure to do this before converting counts to fractions)
    if show_means:
        sums_by_class = _safe_divide(sums_by_class, counts_by_class)
        min_sums_by_class = _safe_divide(min_sums_by_class, counts_by_class)
        max_sums_by_class = _safe_divide(max_sums_by_class, counts_by_class)

    # optional: convert counts to fractions
    if show_percentages:
        factor = np.sum(counts_by_class)
        if factor == 0:
            factor = 1.0  # avoid div-by-zero
        counts_by_class = np.divide(counts_by_class, factor)
        min_counts_by_class = np.divide(min_counts_by_class, factor)
        max_counts_by_class = np.divide(max_counts_by_class, factor)

    # drop anything with zero counts
    summary_mask = counts_by_class > 0
    counts_by_class = counts_by_class[summary_mask]
    sums_by_class = sums_by_class[summary_mask]
    min_counts_by_class = min_counts_by_class[summary_mask]
    max_counts_by_class = max_counts_by_class[summary_mask]
    min_sums_by_class = min_sums_by_class[summary_mask]
    max_sums_by_class = max_sums_by_class[summary_mask]
    terms_list = terms_list[summary_mask]

    # prepare formatting
    def _format_count(count, show_symbol=True):
        if show_percentages and show_symbol:
            return f"{count*100:.1f}%"
        elif show_percentages:
            return f"{count*100:.1f}"
        else:
            return f"{count}"
    sum_symbol = 'x' if (show_means or show_ranges) else '= Σ'
    if show_ranges:
        displayed_sums = np.maximum(abs(min_sums_by_class), abs(max_sums_by_class))
        _, scale = _format_decimal(np.max(displayed_sums), return_scale=True)
    else:
        _, scale = _format_decimal(np.max(abs(sums_by_class)), return_scale=True)

    # format as one-line text description
    summary = ''
    if show_ranges:
        for min_count, max_count, min_sum, max_sum, this_term in zip(min_counts_by_class, max_counts_by_class,
                                                                     min_sums_by_class, max_sums_by_class, terms_list):
            if len(summary) > 0:
                summary += ', '

            # swap pairs of negatives for easier viewing
            if min_sum < max_sum < 0:
                tmp = min_sum
                min_sum = max_sum
                max_sum = tmp

            min_count = _format_count(min_count, show_symbol=False)
            max_count = _format_count(max_count)
            min_sum = _format_decimal(min_sum, significant_digits=4, scale=scale)
            max_sum = _format_decimal(max_sum, significant_digits=4, scale=scale)
            summary += f"{this_term}: {min_count}..{max_count} {sum_symbol} {min_sum}..{max_sum}"
    else:
        for this_count, this_sum, this_term in zip(counts_by_class, sums_by_class, terms_list):
            if len(summary) > 0:
                summary += ', '
            this_count = _format_count(this_count)
            this_sum = _format_decimal(this_sum, significant_digits=4, scale=scale)
            summary += f"{this_term}: {this_count} {sum_symbol} {this_sum}"
    if len(summary) == 0:
        # pretty weird, but maybe this will happen
        summary = '<empty>'

    return summary


# return structure setup for room to grow
def describe_groups(count_groups, sum_groups, term_groups, show_percentages=False, show_means=False,
                    show_ranges=False):
    """
    Constructs a breakdown of information about grouped counts and sums.

    Similar to summarise() but for grouped classifications, and returns a dictionary with a breakdown
    of the information instead of displaying.

    Args:
        count_groups: list of grouped counts, each having shape (g,terms) for different group sizes g
        sum_groups: list of grouped sums, each having shape (g,terms) for different group sizes g
        term_groups: list of grouped term lists, each being a simple list of terms
        show_percentages: bool.
          Whether to show counts as a percentage of all counts being summarised
          (after masking), or as absolute values.
        show_means: bool.
          Whether to compute means from the counts and sums or show the raw sums.
        show_ranges: bool.
          Whether to show min/max range of counts and sums across the value axes,
          or just the sums/means.
          Cannot be combined with show_percentages or show_means.

    Returns:
      dict containing -
        'summaries': textual description of each group
    """

    group_sizes = np.array([counts.shape[0] for counts in count_groups])

    summaries = []
    for counts, sums, terms in zip(count_groups, sum_groups, term_groups):
        # devise original matrix of terms
        value_shape = counts.shape[:-1]
        term_count = len(classify_terms(counts))
        terms_list = [terms[i] if i < len(terms) else '--' for i in range(term_count)]
        terms = np.array(terms_list)
        terms = np.reshape(terms, (1,) * len(value_shape) + (term_count,))
        terms = np.tile(terms, reps=value_shape + (1,))

        # construct summary
        summary = summarise(counts, sums, terms, show_percentages=show_percentages, show_means=show_means,
                            show_ranges=show_ranges)
        summaries.append(summary)

    return {
        'sizes': group_sizes,
        'summaries': summaries
    }


def classify_terms(example=None, retain_shape=False):
    """
    Identifies the appropriate terms list based on the example given, or otherwise
    assumes the terms for a full mat-mul like operation.

    Args:
        example: a count or sum result from a call to matmul_classify() or similar,
            or the tensor containing both the count and sum.
        retain_shape: whether to tile the terms out and to return an array
            in the same shape as the provided example. Only allowed if an example is included.

    Returns:
        list of strings, containing the names of the terms in the default order
        OR
        np-array in same shape as example with identified term for each value
    """
    shape = None
    if example is None:
        tensor_count = 2   # default
    else:
        if isinstance(example, tuple):
            example, _ = example
        shape = np.shape(example)
        channels = shape[-1]
        if channels == 3:
            tensor_count = 1
        elif channels == 9:
            tensor_count = 2
        else:
            raise ValueError("Unrecognised example with {channels} in last dim: {shape}")

    if tensor_count == 1:
        term_list = ['P', 'Z', 'N']
    else:
        term_list = ['PP', 'PZ', 'PN', 'ZP', 'ZZ', 'ZN', 'NP', 'NZ', 'NN']

    if retain_shape:
        if shape is None:
            raise ValueError("Cannot retain shape without example")
        value_shape = shape[:-1]
        term_count = len(term_list)
        term_array = np.array(term_list)
        terms = np.reshape(term_array, (1,) * len(value_shape) + (term_count,))
        terms = np.tile(terms, reps=value_shape + (1,))
        return terms
    else:
        return term_list


def tensor_classify(x, confidence: float = 0.95, threshold: float = None,
                    return_threshold=False):
    """
    Calculates the usual counts and sums of positive, near-zero, and negative values in a single tensor.

    This is an extension from the matmul-like operations to a single tensor.
    It seems somewhat strange on its own, but it proves useful so that you can
    use the other functions to get a nice summary().

    Internally uses TF operations, but returns as numpy for convenience of later processing.

    Args:
        x: np-array or tensor with any shape
        confidence: statistical confidence (0.0 to 1.0) that you wish to meet
            that a value is accurately placed within the P, Z, or N categories.
            Higher values lead to more strict requirements for "near zero".
            1.0 only considers exactly 0.0 as "near zero".
        threshold: abs(x) values less than this are considered near-zero,
            otherwise inferred from confidence
        return_threshold: whether to additionally return the derived threshold

    Returns:
        (counts, sums) containing the counts and sums of each component, respectively.
        Each an np-array with shape: value_shape + (3,).
        OR
        (counts, sums, thresholds) with list of thresholds also returned
    """
    # apply thresholds and create masks
    x = tf.constant(x)
    x_p, x_z, x_n, threshold = classification_mask(x, confidence, threshold)

    # compute counts and sums for each classification
    counts = [
        tf.cast(x_p, tf.float32),
        tf.cast(x_z, tf.float32),
        tf.cast(x_n, tf.float32)]
    sums = [
        tf.where(x_p, x, tf.zeros_like(x)),
        tf.where(x_z, x, tf.zeros_like(x)),
        tf.where(x_n, x, tf.zeros_like(x))]

    # format into final output (numpy)
    counts = np.stack(counts, axis=-1)
    sums = np.stack(sums, axis=-1)
    if return_threshold:
        return counts, sums, threshold.numpy()
    else:
        return counts, sums


def matmul_classify(x, y, confidence: float = 0.95, thresholds: list = None, return_thresholds=False):
    """
    Calculates how the dot-product of x1 . x2 comes to have the range of values that it does.

    For each computation of an output cell, both sets of inputs are independently grouped
    into those that are near-zero (Z), positive at or above threshold (P), and negative at or below threshold (N).
    The output cell value is then defined as the sum of the combinations of those groups, with 9 combinations
    in total, in the following fixed order: PP, PZ, PN, ZP, ZZ, ZN, NP, NZ, NN.

    The counts and sums of each component are returned as separate tensors.
    The original matmul result for any given output cell is given by simply
    summing the sums of each category:
      sum(PP) + sum(PZ) + sum(PN) + ... sum(NN)
    For the entire result this can be computed via:
      output = np.sum(sums, axis=-1)

    "Near zero" is defined as any value having an absolute value less than a threshold.
    By default, the threshold is determined by taking the (1 - zero_confidence) percentile level across each
    of the separate inputs. Alternatively, an absolute magnitude threshold may be specified for
    any of the inputs.

    Internally uses TF operations, but returns as numpy for convenience of later processing.

    Example usage:
    >>> a = np.tile(np.arange(0.0, 1.0, 0.1), (10,1))
    >>> counts, sums = matmul_classify(a, a, confidence=0.90)
    >>> for i, name in enumerate(classify_terms()):
    >>>   if np.sum(counts[:,:,i]) > 0:
    >>>     print(f"Counts({name}): {counts[:,:,i]}")
    >>> for i, name in enumerate(classify_terms()):
    >>>   if np.sum(counts[:,:,i]) > 0:
    >>>     print(f"Sums({name}): {sums[:,:,i]}")
    >>> print(f"True matmul: {np.matmul(a, a)}")
    >>> print(f"Derived matmul: {np.sum(sums, axis=-1)}")

    Args:
      x: np-array or tensor with shape (n, k)
      y: np-array or tensor with shape (k, m)
      confidence: statistical confidence (0.0 to 1.0) that you wish to meet
        that a value is accurately placed within the P, Z, or N categories.
        Higher values lead to more strict requirements for "near zero".
        1.0 only considers exactly 0.0 as "near zero".
        Determines default for either threshold not specified.
      thresholds: list or tuple of 2 floats.
        Specifies explicit thresholds for either or both of the inputs.
        Absolute values less than these thresholds, and values exactly equal to zero,
        are considered "near zero". Otherwise inferred from confidence.
        Various combinations are allowed:
            - None         - both thresholds determined separately, based on confidence.
            - single float - specifies thresholds against both inputs
            - [float, float] - specifies separate thresholds against both inputs
            - [float, None]  - specifies threshold against first only, the other is determined based on confidence
            - [None, float]  - specifies threshold against second only, the other is determined based on confidence
            - [None, None]   - both thresholds determined separately, based on confidence.
      return_thresholds: whether to additionally return the derived thresholds

    Returns:
      (counts, sums) where each is an np-array with shape (n, m, 9)
      OR
      (counts, sums, thresholds) with list of thresholds also returned
    """

    # standardise on data format
    x = tf.constant(x)
    y = tf.constant(y)

    # apply thresholds and create masks
    x_threshold, y_threshold = _parse_thresholds_arg(thresholds)
    x_p, x_z, x_n, x_threshold = classification_mask(x, confidence, x_threshold)
    y_p, y_z, y_n, y_threshold = classification_mask(y, confidence, y_threshold)

    # compute counts
    counts = []
    x_pc = tf.cast(x_p, tf.float32)
    x_zc = tf.cast(x_z, tf.float32)
    x_nc = tf.cast(x_n, tf.float32)
    y_pc = tf.cast(y_p, tf.float32)
    y_zc = tf.cast(y_z, tf.float32)
    y_nc = tf.cast(y_n, tf.float32)
    counts.append(tf.linalg.matmul(x_pc, y_pc))
    counts.append(tf.linalg.matmul(x_pc, y_zc))
    counts.append(tf.linalg.matmul(x_pc, y_nc))
    counts.append(tf.linalg.matmul(x_zc, y_pc))
    counts.append(tf.linalg.matmul(x_zc, y_zc))
    counts.append(tf.linalg.matmul(x_zc, y_nc))
    counts.append(tf.linalg.matmul(x_nc, y_pc))
    counts.append(tf.linalg.matmul(x_nc, y_zc))
    counts.append(tf.linalg.matmul(x_nc, y_nc))

    sums = []
    x_pv = tf.where(x_p, x, tf.zeros_like(x))
    x_zv = tf.where(x_z, x, tf.zeros_like(x))
    x_nv = tf.where(x_n, x, tf.zeros_like(x))
    y_pv = tf.where(y_p, y, tf.zeros_like(y))
    y_zv = tf.where(y_z, y, tf.zeros_like(y))
    y_nv = tf.where(y_n, y, tf.zeros_like(y))
    sums.append(tf.linalg.matmul(x_pv, y_pv))
    sums.append(tf.linalg.matmul(x_pv, y_zv))
    sums.append(tf.linalg.matmul(x_pv, y_nv))
    sums.append(tf.linalg.matmul(x_zv, y_pv))
    sums.append(tf.linalg.matmul(x_zv, y_zv))
    sums.append(tf.linalg.matmul(x_zv, y_nv))
    sums.append(tf.linalg.matmul(x_nv, y_pv))
    sums.append(tf.linalg.matmul(x_nv, y_zv))
    sums.append(tf.linalg.matmul(x_nv, y_nv))

    # format into final output (numpy)
    counts = np.stack(counts, axis=-1)
    sums = np.stack(sums, axis=-1)
    if return_thresholds:
        return counts, sums, [x_threshold.numpy(), y_threshold.numpy()]
    else:
        return counts, sums


def multiply_classify(x, y, confidence: float = 0.95, thresholds: list = None, return_thresholds=False):
    """
    Calculates how the elementwise-product of x (.) y comes to have the range of values that it does.

    Like matmul_classify but for elementwise multiplication.

    Args:
        x: np-array or tensor
        y: np-array or tensor, must have the same type and shape as x

        confidence: statistical confidence (0.0 to 1.0) that you wish to meet
          that a value is accurately placed within the P, Z, or N categories.
          Higher values lead to more strict requirements for "near zero".
          1.0 only considers exactly 0.0 as "near zero".
        thresholds: list or tuple of 2 floats.
            Specifies explicit thresholds for either or both of the inputs.
            Absolute values less than these thresholds, and values exactly equal to zero,
            are considered "near zero". Otherwise inferred from confidence.
            Various combinations are allowed:
                - None         - both thresholds determined separately, based on confidence.
                - single float - specifies thresholds against both inputs
                - [float, float] - specifies separate thresholds against both inputs
                - [float, None]  - specifies threshold against first only, the other is determined based on confidence
                - [None, float]  - specifies threshold against second only, the other is determined based on confidence
                - [None, None]   - both thresholds determined separately, based on confidence.
        return_thresholds: whether to additionally return the derived thresholds

    Returns:
        (counts, sums) containing the counts and sums of each component, respectively.
        Each a tensor with shape `x_shape + (9,)`.
        OR
        (counts, sums, thresholds) with list of thresholds also returned
    """
    # standardise on data format
    x = tf.constant(x)
    y = tf.constant(y)

    # apply thresholds and create masks
    x_threshold, y_threshold = _parse_thresholds_arg(thresholds)
    x_p, x_z, x_n, x_threshold = classification_mask(x, confidence, x_threshold)
    y_p, y_z, y_n, y_threshold = classification_mask(y, confidence, y_threshold)

    # compute counts and sums for each classification
    counts = []
    x_pc = tf.cast(x_p, tf.float32)
    x_zc = tf.cast(x_z, tf.float32)
    x_nc = tf.cast(x_n, tf.float32)
    y_pc = tf.cast(y_p, tf.float32)
    y_zc = tf.cast(y_z, tf.float32)
    y_nc = tf.cast(y_n, tf.float32)
    counts.append(tf.math.multiply(x_pc, y_pc))
    counts.append(tf.math.multiply(x_pc, y_zc))
    counts.append(tf.math.multiply(x_pc, y_nc))
    counts.append(tf.math.multiply(x_zc, y_pc))
    counts.append(tf.math.multiply(x_zc, y_zc))
    counts.append(tf.math.multiply(x_zc, y_nc))
    counts.append(tf.math.multiply(x_nc, y_pc))
    counts.append(tf.math.multiply(x_nc, y_zc))
    counts.append(tf.math.multiply(x_nc, y_nc))

    sums = []
    x_pv = tf.where(x_p, x, tf.zeros_like(x))
    x_zv = tf.where(x_z, x, tf.zeros_like(x))
    x_nv = tf.where(x_n, x, tf.zeros_like(x))
    y_pv = tf.where(y_p, y, tf.zeros_like(y))
    y_zv = tf.where(y_z, y, tf.zeros_like(y))
    y_nv = tf.where(y_n, y, tf.zeros_like(y))
    sums.append(tf.math.multiply(x_pv, y_pv))
    sums.append(tf.math.multiply(x_pv, y_zv))
    sums.append(tf.math.multiply(x_pv, y_nv))
    sums.append(tf.math.multiply(x_zv, y_pv))
    sums.append(tf.math.multiply(x_zv, y_zv))
    sums.append(tf.math.multiply(x_zv, y_nv))
    sums.append(tf.math.multiply(x_nv, y_pv))
    sums.append(tf.math.multiply(x_nv, y_zv))
    sums.append(tf.math.multiply(x_nv, y_nv))

    # format into final output (numpy)
    counts = np.stack(counts, axis=-1)
    sums = np.stack(sums, axis=-1)
    if return_thresholds:
        return counts, sums, [x_threshold.numpy(), y_threshold.numpy()]
    else:
        return counts, sums


def tensordot_classify(x, y, axes, confidence: float = 0.95, thresholds: list = None, return_thresholds=False):
    """
    Calculates how the tensor-dot of x and y come to have the range of values that it does.

    Like matmul_classify but for the tensor-dot operation.

    Args:
        x: np-array or tensor
        y: np-array or tensor, must have the same type and shape as x
        axes: scalar int or a list of int. With same meaning as in tf.tensordot.
        confidence: statistical confidence (0.0 to 1.0) that you wish to meet
          that a value is accurately placed within the P, Z, or N categories.
          Higher values lead to more strict requirements for "near zero".
          1.0 only considers exactly 0.0 as "near zero".
        thresholds: list or tuple of 2 floats.
            Specifies explicit thresholds for either or both of the inputs.
            Absolute values less than these thresholds, and values exactly equal to zero,
            are considered "near zero". Otherwise inferred from confidence.
            Various combinations are allowed:
                - None         - both thresholds determined separately, based on confidence.
                - single float - specifies thresholds against both inputs
                - [float, float] - specifies separate thresholds against both inputs
                - [float, None]  - specifies threshold against first only, the other is determined based on confidence
                - [None, float]  - specifies threshold against second only, the other is determined based on confidence
                - [None, None]   - both thresholds determined separately, based on confidence.
        return_thresholds: whether to additionally return the derived thresholds

    Returns:
        (counts, sums) containing the counts and sums of each component, respectively.
        Each a tensor with shape `value_shape + (9,)`, where `value_shape` is the output shape of the tensor-dot
        operation.
        OR
        (counts, sums, thresholds) with list of thresholds also returned
    """
    # standardise on data format
    x = tf.constant(x)
    y = tf.constant(y)

    # apply thresholds and create masks
    x_threshold, y_threshold = _parse_thresholds_arg(thresholds)
    x_p, x_z, x_n, x_threshold = classification_mask(x, confidence, x_threshold)
    y_p, y_z, y_n, y_threshold = classification_mask(y, confidence, y_threshold)

    # compute counts and sums for each classification
    counts = []
    x_pc = tf.cast(x_p, tf.float32)
    x_zc = tf.cast(x_z, tf.float32)
    x_nc = tf.cast(x_n, tf.float32)
    y_pc = tf.cast(y_p, tf.float32)
    y_zc = tf.cast(y_z, tf.float32)
    y_nc = tf.cast(y_n, tf.float32)
    counts.append(tf.tensordot(x_pc, y_pc, axes=axes))
    counts.append(tf.tensordot(x_pc, y_zc, axes=axes))
    counts.append(tf.tensordot(x_pc, y_nc, axes=axes))
    counts.append(tf.tensordot(x_zc, y_pc, axes=axes))
    counts.append(tf.tensordot(x_zc, y_zc, axes=axes))
    counts.append(tf.tensordot(x_zc, y_nc, axes=axes))
    counts.append(tf.tensordot(x_nc, y_pc, axes=axes))
    counts.append(tf.tensordot(x_nc, y_zc, axes=axes))
    counts.append(tf.tensordot(x_nc, y_nc, axes=axes))

    sums = []
    x_pv = tf.where(x_p, x, tf.zeros_like(x))
    x_zv = tf.where(x_z, x, tf.zeros_like(x))
    x_nv = tf.where(x_n, x, tf.zeros_like(x))
    y_pv = tf.where(y_p, y, tf.zeros_like(y))
    y_zv = tf.where(y_z, y, tf.zeros_like(y))
    y_nv = tf.where(y_n, y, tf.zeros_like(y))
    sums.append(tf.tensordot(x_pv, y_pv, axes=axes))
    sums.append(tf.tensordot(x_pv, y_zv, axes=axes))
    sums.append(tf.tensordot(x_pv, y_nv, axes=axes))
    sums.append(tf.tensordot(x_zv, y_pv, axes=axes))
    sums.append(tf.tensordot(x_zv, y_zv, axes=axes))
    sums.append(tf.tensordot(x_zv, y_nv, axes=axes))
    sums.append(tf.tensordot(x_nv, y_pv, axes=axes))
    sums.append(tf.tensordot(x_nv, y_zv, axes=axes))
    sums.append(tf.tensordot(x_nv, y_nv, axes=axes))

    # format into final output (numpy)
    counts = np.stack(counts, axis=-1)
    sums = np.stack(sums, axis=-1)
    if return_thresholds:
        return counts, sums, [x_threshold.numpy(), y_threshold.numpy()]
    else:
        return counts, sums


def conv_classify(inputs, kernel, strides=1, padding="VALID", confidence: float = 0.95,
                  thresholds: list = None, return_thresholds=False):
    """
    Like matmul_classify but for convolutions.
    Supports 1D, 2D and 3D convolution.

    Args:
        inputs: Tensor of rank N+2. `inputs` has shape
            `(batch_size,) + inputs_spatial_shape + (num_channels,)`
        kernel: Tensor of rank N+2. `kernel` has shape
            `(kernel_spatial_shape, num_input_channels, num_output_channels)`.
            `num_input_channels` should match the number of channels in
            `inputs`.
        strides: int or int tuple/list of `len(inputs_spatial_shape)`,
            specifying the strides of the convolution along each spatial
            dimension. If `strides` is int, then every spatial dimension shares
            the same `strides`.
        padding: string, either `"valid"` or `"same"`. `"valid"` means no
            padding is applied, and `"same"` results in padding evenly to the
            left/right or up/down of the input such that output has the
            same height/width dimension as the input when `strides=1`.
        confidence: statistical confidence (0.0 to 1.0) that you wish to meet
            that a value is accurately placed within the P, Z, or N categories.
            Higher values lead to more strict requirements for "near zero".
            1.0 only considers exactly 0.0 as "near zero".
        thresholds: list or tuple of 2 floats.
            Specifies explicit thresholds for either or both of the inputs.
            Absolute values less than these thresholds, and values exactly equal to zero,
            are considered "near zero". Otherwise inferred from confidence.
            Various combinations are allowed:
                - None         - both thresholds determined separately, based on confidence.
                - single float - specifies thresholds against both inputs
                - [float, float] - specifies separate thresholds against both inputs
                - [float, None]  - specifies threshold against first only, the other is determined based on confidence
                - [None, float]  - specifies threshold against second only, the other is determined based on confidence
                - [None, None]   - both thresholds determined separately, based on confidence.
        return_thresholds: whether to additionally return the derived thresholds

    Returns:
        (counts, sums) containing the counts and sums of each component, respectively.
        Each a tensor with shape `(batch_size,) + inputs_spatial_shape + (num_channels,9)`.
        OR
        (counts, sums, thresholds) with list of thresholds also returned
    """
    # standardise on data format
    inputs = tf.constant(inputs)
    kernel = tf.constant(kernel)

    # apply thresholds and create masks
    inputs_threshold, kernel_threshold = _parse_thresholds_arg(thresholds)
    inputs_p, inputs_z, inputs_n, inputs_threshold = classification_mask(inputs, confidence, inputs_threshold)
    kernel_p, kernel_z, kernel_n, kernel_threshold = classification_mask(kernel, confidence, kernel_threshold)

    # compute counts and sums for each classification
    counts = []
    inputs_pc = tf.cast(inputs_p, tf.float32)
    inputs_zc = tf.cast(inputs_z, tf.float32)
    inputs_nc = tf.cast(inputs_n, tf.float32)
    kernel_pc = tf.cast(kernel_p, tf.float32)
    kernel_zc = tf.cast(kernel_z, tf.float32)
    kernel_nc = tf.cast(kernel_n, tf.float32)
    counts.append(tf.nn.convolution(input=inputs_pc, filters=kernel_pc, strides=strides, padding=padding))
    counts.append(tf.nn.convolution(input=inputs_pc, filters=kernel_zc, strides=strides, padding=padding))
    counts.append(tf.nn.convolution(input=inputs_pc, filters=kernel_nc, strides=strides, padding=padding))
    counts.append(tf.nn.convolution(input=inputs_zc, filters=kernel_pc, strides=strides, padding=padding))
    counts.append(tf.nn.convolution(input=inputs_zc, filters=kernel_zc, strides=strides, padding=padding))
    counts.append(tf.nn.convolution(input=inputs_zc, filters=kernel_nc, strides=strides, padding=padding))
    counts.append(tf.nn.convolution(input=inputs_nc, filters=kernel_pc, strides=strides, padding=padding))
    counts.append(tf.nn.convolution(input=inputs_nc, filters=kernel_zc, strides=strides, padding=padding))
    counts.append(tf.nn.convolution(input=inputs_nc, filters=kernel_nc, strides=strides, padding=padding))

    sums = []
    inputs_pv = tf.where(inputs_p, inputs, tf.zeros_like(inputs))
    inputs_zv = tf.where(inputs_z, inputs, tf.zeros_like(inputs))
    inputs_nv = tf.where(inputs_n, inputs, tf.zeros_like(inputs))
    kernel_pv = tf.where(kernel_p, kernel, tf.zeros_like(kernel))
    kernel_zv = tf.where(kernel_z, kernel, tf.zeros_like(kernel))
    kernel_nv = tf.where(kernel_n, kernel, tf.zeros_like(kernel))
    sums.append(tf.nn.convolution(input=inputs_pv, filters=kernel_pv, strides=strides, padding=padding))
    sums.append(tf.nn.convolution(input=inputs_pv, filters=kernel_zv, strides=strides, padding=padding))
    sums.append(tf.nn.convolution(input=inputs_pv, filters=kernel_nv, strides=strides, padding=padding))
    sums.append(tf.nn.convolution(input=inputs_zv, filters=kernel_pv, strides=strides, padding=padding))
    sums.append(tf.nn.convolution(input=inputs_zv, filters=kernel_zv, strides=strides, padding=padding))
    sums.append(tf.nn.convolution(input=inputs_zv, filters=kernel_nv, strides=strides, padding=padding))
    sums.append(tf.nn.convolution(input=inputs_nv, filters=kernel_pv, strides=strides, padding=padding))
    sums.append(tf.nn.convolution(input=inputs_nv, filters=kernel_zv, strides=strides, padding=padding))
    sums.append(tf.nn.convolution(input=inputs_nv, filters=kernel_nv, strides=strides, padding=padding))

    # format into final output (numpy)
    counts = np.stack(counts, axis=-1)
    sums = np.stack(sums, axis=-1)
    if return_thresholds:
        return counts, sums, [inputs_threshold.numpy(), kernel_threshold.numpy()]
    else:
        return counts, sums


def conv_backprop_filter_classify(x, d_out, kernel_shape, strides=1, padding="VALID", confidence: float = 0.95,
                                  thresholds: list = None, return_thresholds=False):
    """
    Like matmul_classify but for calculation of gradients w.r.t. the filter.
    Supports 1D, 2D and 3D convolution.
    Uses `conv_tools.conv_backprop_filter()` internally.

    Args:
        x: Original input tensor of rank N+2 that had one of the `tf.nn.conv` operations applied against it.
            Shape: `(batch_size, ..spatial_dims.., in_channels)`
        d_out: Tensor of rank N+2, containing the `dJ/dOut` backprop gradients.
            Shape: `(batch_size, ..spatial_dims, out_channels)`
        kernel_shape: N+2 tensor, list, or tuple identifying the shape of the filter used during convolution.
        strides: int or int tuple/list of `len(inputs_spatial_shape)`,
            specifying the strides of the convolution along each spatial
            dimension. If `strides` is int, then every spatial dimension shares
            the same `strides`.
        padding: string, either `"valid"` or `"same"`. `"valid"` means no
            padding is applied, and `"same"` results in padding evenly to the
            left/right or up/down of the input such that output has the
            same height/width dimension as the input when `strides=1`.
        confidence: statistical confidence (0.0 to 1.0) that you wish to meet
            that a value is accurately placed within the P, Z, or N categories.
            Higher values lead to more strict requirements for "near zero".
            1.0 only considers exactly 0.0 as "near zero".
        thresholds: list or tuple of 2 floats.
            Specifies explicit thresholds for either or both of the inputs.
            Absolute values less than these thresholds, and values exactly equal to zero,
            are considered "near zero". Otherwise inferred from confidence.
            Various combinations are allowed:
                - None         - both thresholds determined separately, based on confidence.
                - single float - specifies thresholds against both inputs
                - [float, float] - specifies separate thresholds against both inputs
                - [float, None]  - specifies threshold against first only, the other is determined based on confidence
                - [None, float]  - specifies threshold against second only, the other is determined based on confidence
                - [None, None]   - both thresholds determined separately, based on confidence.
        return_thresholds: whether to additionally return the derived thresholds

    Returns:
        (counts, sums) containing the counts and sums of each component, respectively.
        Each a tensor with shape `(batch_size,) + inputs_spatial_shape + (num_channels,9)`.
        OR
        (counts, sums, thresholds) with list of thresholds also returned
    """
    # standardise on data format
    x = tf.constant(x)
    d_out = tf.constant(d_out)

    # apply thresholds and create masks
    inputs_threshold, d_out_threshold = _parse_thresholds_arg(thresholds)
    inputs_p, inputs_z, inputs_n, inputs_threshold = classification_mask(x, confidence, inputs_threshold)
    d_out_p, d_out_z, d_out_n, d_out_threshold = classification_mask(d_out, confidence, d_out_threshold)

    # compute counts and sums for each classification
    counts = []
    inputs_pc = tf.cast(inputs_p, tf.float32)
    inputs_zc = tf.cast(inputs_z, tf.float32)
    inputs_nc = tf.cast(inputs_n, tf.float32)
    d_out_pc = tf.cast(d_out_p, tf.float32)
    d_out_zc = tf.cast(d_out_z, tf.float32)
    d_out_nc = tf.cast(d_out_n, tf.float32)
    counts.append(ct.conv_backprop_filter(x=inputs_pc, d_out=d_out_pc, kernel_shape=kernel_shape, strides=strides, padding=padding))
    counts.append(ct.conv_backprop_filter(x=inputs_pc, d_out=d_out_zc, kernel_shape=kernel_shape, strides=strides, padding=padding))
    counts.append(ct.conv_backprop_filter(x=inputs_pc, d_out=d_out_nc, kernel_shape=kernel_shape, strides=strides, padding=padding))
    counts.append(ct.conv_backprop_filter(x=inputs_zc, d_out=d_out_pc, kernel_shape=kernel_shape, strides=strides, padding=padding))
    counts.append(ct.conv_backprop_filter(x=inputs_zc, d_out=d_out_zc, kernel_shape=kernel_shape, strides=strides, padding=padding))
    counts.append(ct.conv_backprop_filter(x=inputs_zc, d_out=d_out_nc, kernel_shape=kernel_shape, strides=strides, padding=padding))
    counts.append(ct.conv_backprop_filter(x=inputs_nc, d_out=d_out_pc, kernel_shape=kernel_shape, strides=strides, padding=padding))
    counts.append(ct.conv_backprop_filter(x=inputs_nc, d_out=d_out_zc, kernel_shape=kernel_shape, strides=strides, padding=padding))
    counts.append(ct.conv_backprop_filter(x=inputs_nc, d_out=d_out_nc, kernel_shape=kernel_shape, strides=strides, padding=padding))

    sums = []
    inputs_pv = tf.where(inputs_p, x, tf.zeros_like(x))
    inputs_zv = tf.where(inputs_z, x, tf.zeros_like(x))
    inputs_nv = tf.where(inputs_n, x, tf.zeros_like(x))
    d_out_pv = tf.where(d_out_p, d_out, tf.zeros_like(d_out))
    d_out_zv = tf.where(d_out_z, d_out, tf.zeros_like(d_out))
    d_out_nv = tf.where(d_out_n, d_out, tf.zeros_like(d_out))
    sums.append(ct.conv_backprop_filter(x=inputs_pv, d_out=d_out_pv, kernel_shape=kernel_shape, strides=strides, padding=padding))
    sums.append(ct.conv_backprop_filter(x=inputs_pv, d_out=d_out_zv, kernel_shape=kernel_shape, strides=strides, padding=padding))
    sums.append(ct.conv_backprop_filter(x=inputs_pv, d_out=d_out_nv, kernel_shape=kernel_shape, strides=strides, padding=padding))
    sums.append(ct.conv_backprop_filter(x=inputs_zv, d_out=d_out_pv, kernel_shape=kernel_shape, strides=strides, padding=padding))
    sums.append(ct.conv_backprop_filter(x=inputs_zv, d_out=d_out_zv, kernel_shape=kernel_shape, strides=strides, padding=padding))
    sums.append(ct.conv_backprop_filter(x=inputs_zv, d_out=d_out_nv, kernel_shape=kernel_shape, strides=strides, padding=padding))
    sums.append(ct.conv_backprop_filter(x=inputs_nv, d_out=d_out_pv, kernel_shape=kernel_shape, strides=strides, padding=padding))
    sums.append(ct.conv_backprop_filter(x=inputs_nv, d_out=d_out_zv, kernel_shape=kernel_shape, strides=strides, padding=padding))
    sums.append(ct.conv_backprop_filter(x=inputs_nv, d_out=d_out_nv, kernel_shape=kernel_shape, strides=strides, padding=padding))

    # format into final output (numpy)
    counts = np.stack(counts, axis=-1)
    sums = np.stack(sums, axis=-1)
    if return_thresholds:
        return counts, sums, [inputs_threshold.numpy(), d_out_threshold.numpy()]
    else:
        return counts, sums


def classification_mask(x, confidence: float = 0.95, threshold: float = None):
    """
    Classifies the values of x as positive (P), near-zero (Z), or negative (N) according to a threshold.
    Note: mainly intended for internal use. Returns TF tensors.
    Args:
        x: np-array or tensor
        confidence: statistical confidence (0.0 to 1.0) that you wish to meet
            that a value is accurately placed within the P, Z, or N categories.
            Higher values lead to more strict requirements for "near zero".
            1.0 only considers exactly 0.0 as "near zero".
        threshold: abs(x) values less than this are considered near-zero,
            otherwise inferred by using confidence to draw an appropriate percentile from the
            values of x.
    Returns:
        (pos_mask, zero_mask, neg_mask, threshold) - bool TF tensors, plus the derived threshold

    """
    # determine threshold
    if threshold is None:
        threshold = tfp.stats.percentile(tf.abs(x), 100 * (1 - confidence), interpolation='midpoint')

    # apply threshold
    # Note: on small matrices with few discrete numbers, percentile() will find a value on either side
    #  of the percentage threshold, thus we should apply the threshold rule:
    #     - zero if value < threshold
    # However, the threshold may be zero, which requires the extra rule:
    #     - zero if zero
    zero_mask = tf.logical_or(x == 0, tf.abs(x) < threshold)
    pos_mask = tf.logical_and(x > 0, tf.logical_not(zero_mask))
    neg_mask = tf.logical_and(x < 0, tf.logical_not(zero_mask))

    return pos_mask, zero_mask, neg_mask, threshold


# It turns out that I shouldn't have bothered trying to sort the order of the final groups.
# It would have been more consistent to apply the group ordering in filter_groups().
# Furthermore, doing the group ordering AFTER grouping is a lot easier and probably more efficient.
def group_classifications(counts, sums, terms=None, mask=None):
    """
    Args:
        counts: classified counts, with shape: value_shape + (terms,)
        sums: classified sums, with shape: value_shape + (terms,)
        terms: ordered terms, with shape: value_shape + (terms,).
            Must be supplied if counts and sums have been filtered and sorted via filter_classifications().
        mask: boolean mask to select just certain counts and sums, with shape: value_shape.
            Otherwise calculates groups across the entire set of data.

    Returns:
        - count_groups - list of grouped counts, each having shape (g,terms) for different group sizes g
        - sum_groups - list of grouped sums, each having shape (g,terms) for different group sizes g
        - term_groups - list of grouped term lists, each being a simple list of terms
    """
    # sanity checks
    if not np.all(counts.shape == sums.shape):
        raise ValueError(f"counts and sums have different shapes: {counts.shape} != {sums.shape}")
    if terms is not None and not np.all(terms.shape == counts.shape):
        raise ValueError(f"terms must be same shape as counts and sums. Expected {counts.shape}, got {terms.shape}")
    if mask is not None and not np.all(mask.shape == counts.shape[:-1]):
        raise ValueError(
            f"mask must match value shape of counts and sums. Expected {counts.shape[:-1]}, got {mask.shape}")

    # prepare data
    # - default terms if not provided
    # - apply mask if provided
    # - flatten everything into shape (n,terms)
    if terms is None:
        terms = classify_terms(counts, retain_shape=True)
    if mask is None:
        num_terms = counts.shape[-1]
        counts = counts.reshape(-1, num_terms)
        sums = sums.reshape(-1, num_terms)
        terms = terms.reshape(-1, num_terms)
    else:
        counts = counts[mask]
        sums = sums[mask]
        terms = terms[mask]

    # compute grouping values
    # - convert order of terms into a single number by treating the terms axis as a base-(9+1) number system,
    #   largest digit at front, with zero meaning "no term" due to zero count.
    # - any zero-count terms are also zerod out, and the final sum across terms is now a descriptor for that pattern
    #   of terms.
    terms_list = classify_terms(counts)
    term_number_lookup = {term: i + 1 for i, term in enumerate(terms_list)}
    term_numbers = np.vectorize(term_number_lookup.get)(terms)
    term_numbers[counts == 0] = 0
    term_scales = [pow(len(terms_list), len(terms_list) - i - 1) for i in range(len(terms_list))]
    grouping_values = np.sum(np.multiply(term_numbers, term_scales), axis=-1)  # (n,) x group value

    # sort into groups, largest groups first
    # - compute frequency counts across unique grouping values
    # - sort all values into those groups in that order
    unique_grouping_values, grouping_value_counts = np.unique(grouping_values, return_counts=True)
    sort_order = np.argsort(-grouping_value_counts, kind='stable')  # descending order, otherwise retaining order
    unique_grouping_values = unique_grouping_values[sort_order]  # (g,) x group value
    value_to_sorted_position = {value: idx for idx, value in enumerate(unique_grouping_values)}
    sortable_ids = np.vectorize(value_to_sorted_position.get)(grouping_values)  # (n,) x index into (g,)
    sort_order = np.argsort(sortable_ids, kind='stable')  # (n,) x index into (n,)
    counts = counts[sort_order]
    sums = sums[sort_order]
    terms = terms[sort_order]
    grouping_values = grouping_values[sort_order]

    # split into groups
    # - identify indices at start of each group
    # - split everything according to that grouping
    # - note: by this point there may be some variation in the total counts
    #   but only because any initial filtering before values were supplied
    #   to this function. So, on average, each position will have the same
    #   total count, and thus the only measure of coverage for a given group
    #   is the size of the group.
    indices = np.where(np.diff(grouping_values) != 0)[0] + 1
    count_groups = np.split(counts, indices)  # k x (
    sum_groups = np.split(sums, indices)
    term_groups = np.split(terms, indices)

    # convert term np-arrays to lists
    # - each group now has the same terms in the same order, but only in relation to the counts
    #   that have been kept after filtering.
    # - so resolve all that and turn each a simple list of terms for each group
    for grp_idx, (counts, terms) in enumerate(zip(count_groups, term_groups)):
        term_len = np.sum(np.max(counts != 0, axis=0))
        terms_list = list(terms[0][0:term_len])
        term_groups[grp_idx] = terms_list

    return count_groups, sum_groups, term_groups


def filter_classifications(counts, sums, completeness=0.75):
    """
    Filters classification data in order to retain a certain
    "completeness of explanatory coverage". This eliminates less important noise,
    making it easier to understand any summaries produced from it.
    It is also useful for grouping similar classification results that have
    the same major structure, while ignoring less important noise.

    The final returned counts, sums, and terms are sorted for descending
    counts.

    Everything is determined independently for each position
    within the original value tensor, including for final sort. Thus the result
    also includes a terms tensor, with the same shape as counts and sums,
    in order to identify the final terms order for each position.

    Args:
        counts: the counts returned by matmul_classify() or one of its variants.
        sums: the sums returned by matmul_classify() or one of its variants.
        completeness: float in range 0.0 to 1.0.
            Minimum required "completeness of explanatory coverage".
            After filtering, the retained counts and sums will explain at least this much
            of the final result, measured as a combined fraction of the total number of counts
            and the maximum positive or negative extent of the sums.
    Returns:
        (counts, sums, terms) - sorted and filtered (less important counts and sums zerod-out)
    """
    # steps:
    # - initialise a full tuple of (counts, sums, terms, masks) that will always be sorted
    #   together simultaneously
    # - do a sort and filter by sums first
    #   - while the final output is primarily by count, sometimes there are a small number
    #     of unusually large values and we want to see those too
    # - then do a sort and filter by counts
    # - combine final result masks
    # - set all rejected values to zeros
    # - final result is sorted according to counts, largest first
    terms = classify_terms(counts, retain_shape=True)
    masks = np.zeros_like(counts, dtype=bool)
    counts, sums, terms, masks = _partial_filter_by_sum(counts, sums, terms, masks, completeness)
    counts, sums, terms, masks = _partial_filter_by_count(counts, sums, terms, masks, completeness)

    # apply masks - zero-out discarded counts and sums
    # (Must retain terms. I had initially replaced masked terms with '--',
    #  but that causes problems when later needing to standardize the order for
    #  summarisation.)
    counts = counts * masks
    sums = sums * masks

    # apply final sorting - by descending count after masking
    # - although _partial_filter_by_count() will have returned things in the right sort order,
    #   now that we've applied the mask, some of the counts have changed to zero and the order
    #   isn't now quite accurate.
    # - for example, if there's a low-count high-sum, it'll be at the end somewhere
    #   and now needs to come further forward.
    sort_order = np.argsort(-counts, axis=-1)  # negate for descending order
    counts = np.take_along_axis(counts, sort_order, axis=-1)
    sums = np.take_along_axis(sums, sort_order, axis=-1)
    terms = np.take_along_axis(terms, sort_order, axis=-1)
    return counts, sums, terms


def filter_groups(count_groups, sum_groups, term_groups, completeness=0.75, max_groups=None, return_coverage=False):
    """
    Filters grouped counts and sums in order to retain a maximum "completeness of explanatory coverage" across the groups.
    Assumes that the groups have already been sorted into descending order of significance,
    as returned by group_classifications().

    Args:
        count_groups: list of grouped counts, each having shape (g,terms) for different group sizes g
        sum_groups: list of grouped sums, each having shape (g,terms) for different group sizes g
        term_groups: list of grouped term lists, each being a simple list of terms
        completeness: the smallest groups are filtered and discarded in order to meet at most this target fraction of
          explanatory coverage. Supply 1.0 or None to avoid filtering.
        max_groups: if specified, the smallest groups are filtered and discarded in order to retain at most this many groups.
        return_coverage: bool.
          Whether to additional return the actual coverage after filtering.

    Returns:
        - count_groups - filtered count groups
        - sum_groups - filtered sum groups
        - term_groups - filtered term groups
        - coverage - (optional) actual level of completeness achieved
    """
    # handle args
    if completeness is None:
        completeness = 1.0

    # setup
    group_sizes = np.array([counts.shape[0] for counts in count_groups])
    total = np.sum(group_sizes)
    threshold = total * (1 - completeness)

    # apply threshold
    accumed = np.cumsum(group_sizes[::-1])[::-1]  # cumsum in reverse direction
    mask = accumed >= threshold
    indices = np.nonzero(mask)[0]

    # apply max_groups
    if max_groups and len(indices) > max_groups:
        indices = indices[:max_groups]

    # apply filtering
    coverage = np.sum(group_sizes[indices]) / total
    count_groups = [count_groups[i] for i in indices]
    sum_groups = [sum_groups[i] for i in indices]
    term_groups = [term_groups[i] for i in indices]

    if return_coverage:
        return count_groups, sum_groups, term_groups, coverage
    else:
        return count_groups, sum_groups, term_groups


# TODO maybe update so it can also accept a terms-list for re-ordering
def standardize(counts, sums=None, terms=None, *, mask=None, return_terms_as_list=True):
    """
    Takes a set of counts, sums, and optional terms and standardizes their order
    and container types for easier handling.

    If masking is requested, the un-masked counts and sums are zerod out.
    No masking is done to the terms.

    Args:
        counts: the counts returned by matmul_classify() or one of its variants.
          Shape: value_shape + (terms,)
          Alternatively pass a tuple containing the counts and sums, and optionally
          the terms.
        sums: the sums returned by matmul_classify() or one of its variants.
          Shape: value_shape + (terms,)
        terms: terms returned by filter_classifications(), same shape as counts and sums.
          Shape: value_shape + (terms,)
          Must be included if filter_classifications() has been called.
        mask: bool with shape: value_shape.
        return_terms_as_list: bool.
            Whether to return the terms component as a list, otherwise returns
            as a matrix of shape: value_shape + (terms,)

    Returns:
        - counts - counts array with standardized order, shape: value_shape + (terms,)
        - sums - sums array with standardized order, shape: value_shape + (terms,)
        - terms - simple terms list OR full terms array, shape: value_shape + (terms,)
    """
    # parse args
    if isinstance(counts, tuple):
        if len(counts) == 2:
            counts, sums = counts
        else:
            counts, sums, terms = counts

    # sanity checks
    if not np.all(counts.shape == sums.shape):
        raise ValueError(f"counts and sums have different shapes: {counts.shape} != {sums.shape}")
    if terms is not None and not np.all(terms.shape == counts.shape):
        raise ValueError(f"terms must be same shape as counts and sums. Expected {counts.shape}, got {terms.shape}")
    if mask is not None and not np.all(mask.shape == counts.shape[:-1]):
        raise ValueError(f"mask must match value shape of counts and sums. "
                         f"Expected {counts.shape[:-1]}, got {mask.shape}")

    # standardize on type
    counts = np.array(counts)
    sums = np.array(sums)
    if terms is not None:
        terms = np.array(terms)
    if mask is not None:
        mask = np.array(mask)

    # cleanup order for consistent order by terms
    if terms is not None:
        counts, sums, terms = _standardize_order(counts, sums, terms)
    elif not return_terms_as_list:
        terms = classify_terms(counts, retain_shape=True)
    terms_list = classify_terms(counts)

    # apply mask
    if mask is not None:
        counts = counts * mask[..., np.newaxis]
        sums = sums * mask[..., np.newaxis]

    # construct final terms result
    if return_terms_as_list:
        return counts, sums, terms_list
    else:
        return counts, sums, terms


def _parse_thresholds_arg(thresholds):
    """
    Handles the variations allowed for the `thresholds` arg to the xxx_classify() methods.
    Args:
      thresholds: list or tuple of 2 floats, or None.
        Specifies explicit thresholds for either or both of the inputs.
        Absolute values less than these thresholds, and values exactly equal to zero,
        are considered "near zero". Otherwise inferred from confidence.
        Various combinations are allowed:
            - None         - both thresholds determined separately, based on confidence.
            - single float - specifies thresholds against both inputs
            - [float, float] - specifies separate thresholds against both inputs
            - [float, None]  - specifies threshold against first only, the other is determined based on confidence
            - [None, float]  - specifies threshold against second only, the other is determined based on confidence
            - [None, None]   - both thresholds determined separately, based on confidence.
    Returns:
        tuple (threshold1, threshold2)
    """
    t1 = None
    t2 = None
    if thresholds is not None:
        try:
            t1, t2 = thresholds
        except ValueError:
            raise ValueError(f"thresholds must have length 2, but got length {len(thresholds)}: {thresholds}")
    return t1, t2


def _partial_filter_by_sum(counts, sums, terms, masks, completeness):
    """
    Internal method for use by filter_classifications().

    Applies a sort and filter over the provided counts, sums, terms and masks.
    All are sorted according to descending sum order.
    Masks are updated to indicate values that must be retained.
    Args:
        counts: counts tensor from a classification function, with shape (value_shape + (terms,))
        sums: sums tensor from a classification function, with shape (value_shape + (terms,))
        terms: terms tensor corresponding to the counts and sums, with shape (value_shape + (terms,))
        masks: boolean mask indicating values to retain, with shape (value_shape + (terms,))
            Input value will either be filled with False, or indicate values that must be retained
            as determined by prior filter logic.
        completeness: float in range 0.0 to 1.0.
            Determines minimum fraction of total range that must be retained.
    Returns:
        (counts, sums, terms, masks) with all values sorted along terms dimension, and masks updated
            with additional entries that must be retained
    """
    # determine thresholds against sums
    pos_range = np.sum((sums * (sums > 0)), axis=-1)
    neg_range = np.abs(np.sum((sums * (sums < 0)), axis=-1))
    abs_range = np.maximum(pos_range, neg_range)
    threshold = abs_range * (1 - completeness)  # shape: value_shape

    # sort everything according to sum magnitudes, smallest first for cumsum
    sort_order = np.argsort(np.abs(sums), axis=-1)
    counts = np.take_along_axis(counts, sort_order, axis=-1)
    sums = np.take_along_axis(sums, sort_order, axis=-1)
    terms = np.take_along_axis(terms, sort_order, axis=-1)
    masks = np.take_along_axis(masks, sort_order, axis=-1)  # shape: value_shape + (terms,)

    # update masks
    # - starting from front, sums up positive and negative "marginal" sums separately
    # - the threshold point is the point where the max of the absolute margins >= threshold
    pos_margin_sums = np.cumsum(sums * (sums > 0), axis=-1)
    neg_margin_sums = np.cumsum(sums * (sums < 0), axis=-1)
    mag_margin_sums = np.maximum(pos_margin_sums, np.abs(neg_margin_sums))  # shape: value_shape + (terms,)
    threshold_mask = mag_margin_sums >= threshold[..., np.newaxis]  # shape: value_shape + (terms,)
    masks = np.logical_or(threshold_mask, masks)

    # flip final results for largest-to-smallest order
    counts = np.flip(counts, axis=-1)
    sums = np.flip(sums, axis=-1)
    terms = np.flip(terms, axis=-1)
    masks = np.flip(masks, axis=-1)
    return counts, sums, terms, masks


def _partial_filter_by_count(counts, sums, terms, masks, completeness):
    """
    Internal method for use by filter_classifications().

    Applies a sort and filter over the provided counts, sums, terms and masks.
    All are sorted according to descending count order.
    Masks are updated to indicate values that must be retained.
    Args:
        counts: counts tensor from a classification function, with shape (value_shape + (terms,))
        sums: sums tensor from a classification function, with shape (value_shape + (terms,))
        terms: terms tensor corresponding to the counts and sums, with shape (value_shape + (terms,))
        masks: boolean mask indicating values to retain, with shape (value_shape + (terms,))
            Input value will either be filled with False, or indicate values that must be retained
            as determined by prior filter logic.
        completeness: float in range 0.0 to 1.0.
            Determines minimum fraction of total range that must be retained.
    Returns:
        (counts, sums, terms, masks) with all values sorted along terms dimension, and masks updated
            with additional entries that must be retained
    """
    # determine thresholds against counts
    threshold = np.sum(counts, axis=-1) * (1 - completeness)

    # sort everything according to counts, smallest first for cumsum
    sort_order = np.argsort(counts, axis=-1)
    counts = np.take_along_axis(counts, sort_order, axis=-1)
    sums = np.take_along_axis(sums, sort_order, axis=-1)
    terms = np.take_along_axis(terms, sort_order, axis=-1)
    masks = np.take_along_axis(masks, sort_order, axis=-1)  # shape: value_shape + (terms,)

    # update masks
    # - starting from front, sums up "marginal" counts
    # - the threshold point is the point where the margins >= threshold
    margin_counts = np.cumsum(counts, axis=-1)  # shape: value_shape + (terms,)
    threshold_mask = margin_counts >= threshold[..., np.newaxis]  # shape: value_shape + (terms,)
    masks = np.logical_or(threshold_mask, masks)

    # flip final results for largest-to-smallest order
    counts = np.flip(counts, axis=-1)
    sums = np.flip(sums, axis=-1)
    terms = np.flip(terms, axis=-1)
    masks = np.flip(masks, axis=-1)
    return counts, sums, terms, masks


def _fixargsort(a, reference, axis=-1):
    """
    Like np.argsort() but that it returns the indices needed to "fix" the sort order of the given
    list or array so that it has the same order as reference.
    Assumes that both lists or arrays are of the same shape and have the same values, just in different orders.
    Args:
      a: a list or array needing to have its order "fixed"
      reference: the 1D list or array with the reference order
      axis: int or None, optional. Axis along which to sort. The default is -1 (the last axis). If None, the flattened array is used.
    Returns:
      indices for sorting 'a'
    """
    # For very simple lists, this function would look like the following:
    #  a, reference = np.array(a), np.array(reference)
    #  ref_meta_order = np.argsort(np.argsort(reference))
    #  a_order = np.argsort(a)
    #  return a_order[ref_meta_order]
    # Everything else you see here is there in order to cope with arrays
    # and with variations in how the reference is supplied.

    # normalize types
    a = np.array(a)
    reference = np.array(reference)

    # tile reference out to match shape of a
    if reference.ndim < a.ndim and axis is not None:
        reshape_shape = [1] * len(a.shape)
        reshape_shape[axis] = len(reference)
        reps = list(a.shape)
        reps[axis] = 1
        reference = np.reshape(reference, reshape_shape)
        reference = np.tile(reference, reps)

    # get meta-order from reference
    ref_meta_order = np.argsort(np.argsort(reference, axis=axis), axis=axis)

    # determine ordering of a
    a_order = np.argsort(a, axis=axis)
    indices = np.take_along_axis(a_order, ref_meta_order, axis=axis)
    return indices



def _standardize_order(counts, sums, terms):
    """
    Reverses the ordering effects of filter_classifications().
    Args:
        counts, sums, terms - all must have same shape: value_shape + (terms,)
    Returns:
        counts, sums, terms - reordered along terms axis so that all entries all in the same
            order as returned by classify_terms().
    """
    # sanity checks
    if not np.all(counts.shape == sums.shape):
        raise ValueError(f"counts and sums have different shapes: {counts.shape} != {sums.shape}")
    if terms is not None and not np.all(terms.shape == counts.shape):
        raise ValueError(f"terms must be same shape as counts and sums. Expected {counts.shape}, got {terms.shape}")

    # re-order
    terms_list = classify_terms(counts)
    sort_order = _fixargsort(terms, terms_list, axis=-1)
    counts = np.take_along_axis(counts, sort_order, axis=-1)
    sums = np.take_along_axis(sums, sort_order, axis=-1)
    terms = np.take_along_axis(terms, sort_order, axis=-1)

    return counts, sums, terms


def _safe_divide(x, y):
    """
    Element-wise divide x by y, or zero if y is zero.
    Intended as a div-by-zero-safe version for computing means from a sums and counts.
    """
    return np.divide(x, y, out=np.zeros_like(x, dtype=float), where=(y != 0))


def _format_decimal(value, significant_digits=4, scale=None, return_scale=False):
    """
    Variant of the standard number formatting that is optimised first for easier visual comparison
    across multiple numbers potentially ranging wildly across different scales,
    and for compactness second.
    This is achieved by targeting the number of displayed significant digits, regardless
    of scale, and by avoiding scientific notation except for the largest values.

    Can be used to construct a shared scale across multiple numbers, eg:
    > max_value = np.max(abs(values))
    > _, scale = _format_decimal(max_value, return_scale=True)
    > formatted = [_format_decimal(value, scale=scale) for value in values]

    Args:
      value: the value to format
      significant_digits: number of non-zero digits wanted for display
      scale: use this scale instead of calculating from the given value.
    """
    if scale is None:
        scale = 0 if value == 0 else int(np.floor(np.log10(abs(value))))

    if scale < 0:
        p = significant_digits - scale + 1  # more digits as the number gets smaller
        res = f"{value:.{p}f}"
    else:
        p = max(0, significant_digits - scale - 1)  # less digits as the number gets larger
        res = f"{value:.{p}f}"

    # todo: maybe switch to scientific notation if length of standard display is some multiple of the length
    # of scientific notation

    if return_scale:
        return res, scale
    else:
        return res
