#!/usr/local/bin/python3.4
#
# step5.py
#
# David Jones, Ravenbrook Limited, 2009-10-27
# Avi Persin, Revision 2016-01-06

"""
Step 5 of the GISTEMP algorithm.

In Step 5: 8000 subboxes are combined into 80 boxes, and ocean data is
combined with land data; boxes are combined into latitudinal zones
(including hemispheric and global zones); annual and seasonal anomalies
are computed from monthly anomalies.
"""

import os

import parameters
from settings import *
from steps import eqarea, giss_data, series
from steps.giss_data import valid, MISSING
from steps.step3 import asjson
from tool import gio

log = open(os.path.join(LOG_DIR, "step5.log"), "w")


def as_boxes(data):
    """Wrapper for *land_ocean_boxes*."""
    meta = next(data)
    return land_ocean_boxes(meta, data)


def land_ocean_boxes(meta, cells):
    """From the input data, *cells*, 3 separate series of boxes are
    made: one for a land-only series, one for an ocean-only series, and
    one mixed series (the usual analysis).

    *meta* is a triple of (mask,land,ocean) meta data.

    *cells* should be an iterator of (weight, land, ocean) triples
    (a land and ocean series for each subbox).  *weight* is used by the
    mixed series: when *weight* is 1 the land series is selected;
    when *weight* is 0 the ocean series is selected.  Currently no
    intermediate weights are supported.  Typically these weights are
    generated by `ensure_weight`.

    A triple of (meta,data) pairs is returned.
    """

    mask_meta, land_meta, ocean_meta = meta
    begin_year = str(1880)
    end_year = ocean_meta.title.decode().strip()[-4:]

    land_file = open(
        RESULT_DIR + "GHCNv4BoxesLand." + str(int(land_meta.gridding_radius)) + ".txt",
        "w",
    )
    print("GHCNv3 Temperature Anomalies (C) Land Only", file=land_file)
    print(
        begin_year
        + " "
        + end_year
        + " 9999.0 "
        + str(int(land_meta.gridding_radius))
        + " (=first year, last year, missing data flag, smoothing radius)",
        file=land_file,
    )

    assert land_meta.mavg == 6
    land_meta.mode = "land"
    ocean_meta.mode = "ocean"

    # List of cells for each series.
    land = []
    mixed = []

    # It's a mistake to do the land--ocean mixed analysis using land
    # data up to 2010-12 and ocean data only up to 2010-11 (say).
    # We detect that here, by keeping track of the the min and max
    # months (with data) for the land cells and ocean cells that are
    # used by the mixed series.
    minland = 999999
    maxland = -999999
    minocean = 999999
    maxocean = -999999
    for landweight, landcell, oceancell in cells:
        land.append(landcell)
        # Simple version of mixed selects either land or ocean.
        assert landweight in (0, 1)
        if landweight:
            mixedcell = landcell
            minland = min(minland, landcell.first_valid_month())
            maxland = max(maxland, landcell.last_valid_month())
        else:
            mixedcell = oceancell
            minocean = min(minocean, oceancell.first_valid_month())
            maxocean = max(maxocean, oceancell.last_valid_month())
        mixed.append(mixedcell)

        if landcell != "":
            print(*landcell.box + [landcell.d], file=land_file)
            for entry in landcell.get_set_of_years(1880, int(end_year)):
                entry = [round(x, 5) for x in entry]
                print(*entry, file=land_file)

    del landweight, landcell, oceancell
    if (minland, maxland) != (minocean, maxocean):
        warn_land_ocean(minland, maxland, minocean, maxocean)

    def iterland():
        return land_meta, subbox_to_box(land_meta, land, celltype="LND")

    def itermixed():
        first_year = min(land_meta.yrbeg, ocean_meta.yrbeg)
        land_limit_year = land_meta.yrbeg + land_meta.monm // 12
        ocean_limit_year = ocean_meta.yrbeg + ocean_meta.monm // 12
        limit_year = max(land_limit_year, ocean_limit_year)
        max_months = (limit_year - first_year) * 12

        # For the metadata for the mixed analysis, start with a copy of
        # the land metadata.
        mixed_meta = giss_data.StationMetaData(
            land_month_range=(minland, maxland),
            ocean_month_range=(minocean, maxocean),
            **land_meta.__dict__
        )

        mixed_meta.yrbeg = first_year
        mixed_meta.monm = max_months
        mixed_meta.mode = "mixed"
        mixed_meta.ocean_source = ocean_meta.ocean_source
        year_min = (min(minocean, minland) - 1) // 12
        year_max = (max(maxocean, maxland) - 1) // 12
        mixed_meta.title = (
            "Combined Land--Ocean Temperature Anomaly (C) CR %4dkm %s to %s."
            % (land_meta.gridding_radius, str(year_min), str(year_max))
        )
        land_meta.months_data = max(maxland, maxocean)
        mixed_meta.months_data = max(maxland, maxocean)
        return mixed_meta, subbox_to_box(mixed_meta, mixed, celltype="MIX")

    return iterland(), itermixed()


def warn_land_ocean(*l):
    """Produce a warning about mismatched land/ocean data ranges."""

    def iso8601(x):
        """Produce string of form YYYY-MM."""

        y, m = divmod(x - 1, 12)
        m += 1
        return "%04d-%02d" % (y, m)

    print(
        "WARNING: Bad mix of land and ocean data.\n"
        "  Land range from %s to %s; Ocean range from %s to %s."
        % tuple(map(iso8601, l))
    )


def subbox_to_box(meta, cells, celltype="BOX"):
    """Aggregate the subboxes (aka cells, typically 8000 per globe)
    into boxes (typically 80 boxes per globe), and combine records to
    produce one time series per box.

    *celltype* is used for logging, using a distinct (3 character) code
    will allow the log output for the land, ocean, and land--ocean
    analyses to be separated.

    *meta* specifies the meta data and is used to determine the first
    year (meta.yrbeg) and length (meta.monm) for all the resulting
    series.

    Returns an iterator of box data: for each box a quadruple of
    (*anom*, *weight*, *ngood*, *box*) is yielded.  *anom* is the
    temperature anomaly series, *weight* is the weights for the series
    (number of cells contributing for each month), *ngood* is total
    number of valid data in the series, *box* is a 4-tuple that
    describes the regions bounds: (southern, northern, western, eastern).
    """

    # The (80) large boxes.
    boxes = list(eqarea.grid())
    # For each box, make a list of contributors (cells that contribute
    # to the box time series); initially empty.
    contributordict = dict((box, []) for box in boxes)

    # Partition the cells into the boxes.
    for cell in cells:
        box = whichbox(boxes, cell.box)
        contributordict[box].append(cell)

    def padded_series(s):
        """Produce a series, that is padded to start in meta.yrbeg and
        is of length meta.monm months.
        *s* should be a giss_data.Series instance.
        """

        result = [MISSING] * meta.monm
        offset = 12 * (s.first_year - meta.yrbeg)
        result[offset : offset + len(s)] = s.series
        return result

    # For each box, sort and combine the contributing cells, and output
    # the result (by yielding it).
    for idx, box in enumerate(boxes):
        contributors = sorted(
            contributordict[box], key=lambda x: x.good_count, reverse=True
        )

        best = contributors[0]
        box_series = padded_series(best)
        box_weight = [float(valid(a)) for a in box_series]

        # Start the *contributed* list with this cell.
        l = [any(valid(v) for v in box_series[i::12]) for i in range(12)]
        s = "".join("01"[x] for x in l)
        contributed = [[best.uid, 1.0, s]]
        # Loop over the remaining contributors.
        for cell in contributors[1:]:
            if cell.good_count >= parameters.subbox_min_valid:
                addend_series = padded_series(cell)

                weight = 1.0
                station_months = series.combine(
                    box_series,
                    box_weight,
                    addend_series,
                    weight,
                    parameters.box_min_overlap,
                )
                s = "".join("01"[bool(x)] for x in station_months)
            else:
                weight = 0.0
                s = "0" * 12
            contributed.append([cell.uid, weight, s])
        box_first_year = meta.yrbeg
        series.anomalize(box_series, parameters.subbox_reference_period, box_first_year)
        uid = giss_data.boxuid(box, celltype=celltype)
        log.write("%s cells %s\n" % (uid, asjson(contributed)))
        ngood = sum(valid(a) for a in box_series)

        yield (box_series, box_weight, ngood, box)


def whichbox(boxes, cell):
    """Return the box in *boxes* that contains (the centre of the)
    *cell*.
    """

    lat, lon = eqarea.centre(cell)
    for box in boxes:
        s, n, w, e = box
        if s <= lat < n and w <= lon < e:
            return box


def zonav(meta, boxed_data):
    """Zonal Averaging.

    The input *boxed_data* is an iterator of boxed time series.
    The data in the boxes are combined to produce averages over
    various latitudinal zones.  Returns an iterator of
    (averages, weights, title) tuples, one per zone.

    16 zones are produced.  The first 8 are the basic belts that are used
    for the equal area grid, the remaining 8 are combinations:

      0 64N - 90N               \
      1 44N - 64N (asin 0.9)     -  8 24N - 90 N  (0 + 1 + 2)
      2 24N - 44N (asin 0.7)    /
      3 Equ - 24N (asin 0.4)    \_  9 24S - 24 N  (3 + 4)
      4 24S - Equ               /
      5 44S - 24S               \
      6 64S - 44S                - 10 90S - 24 S  (5 + 6 + 7)
      7 90S - 64S               /

     11 northern mid-latitudes (1 + 2)
     12 southern mid-latitudes (5 + 6)
     13 northern hemisphere (0 + 1 + 2 + 3)
     14 southern hemisphere (4 + 5 + 6 + 7)
     15 global (all belts 0 to 7)
    """

    iyrbeg = meta.yrbeg
    monm = meta.monm

    boxes_in_band, band_in_zone = zones()

    bands = len(boxes_in_band)

    lenz = [None] * bands
    wt = [None] * bands
    avg = [None] * bands
    # For each band, combine all the boxes in that band to create a band
    # record.
    for band in range(bands):
        # The temperature (anomaly) series for each of the boxes in this
        # band.
        box_series = [None] * boxes_in_band[band]
        # The weight series for each of the boxes in this band.
        box_weights = [None] * boxes_in_band[band]
        # "length" is the number of months (with valid data) in the box
        # series.  For each box in this band.
        box_length = [None] * boxes_in_band[band]

        for box in range(boxes_in_band[band]):
            # The last element in the tuple is the boundaries of the
            # box.  We ignore it.
            box_series[box], box_weights[box], box_length[box], _ = next(boxed_data)

        # total number of valid data in band's boxes
        total_length = sum(box_length)

        if total_length == 0:
            wt[band] = [0.0] * monm
            avg[band] = [MISSING] * monm
        else:
            box_length, IORD = sort_perm(box_length)
            nr = IORD[0]

            # Copy the longest box record into *wt* and *avg*.
            # Using list both performs a copy and converts into a mutable
            # list.
            wt[band] = list(box_weights[nr])
            avg[band] = list(box_series[nr])

            # And combine the remaining series.
            for n in range(1, boxes_in_band[band]):
                nr = IORD[n]
                if box_length[n] == 0:
                    # Nothing in this box, and since we sorted by length,
                    # all the remaining boxes will also be empty.  We can
                    # stop combining boxes.
                    break

                series.combine(
                    avg[band],
                    wt[band],
                    box_series[nr],
                    box_weights[nr],
                    parameters.box_min_overlap,
                )
        series.anomalize(avg[band], parameters.box_reference_period, iyrbeg)
        lenz[band] = sum(valid(a) for a in avg[band])

        yield (avg[band], wt[band])

    # We expect to have consumed all the boxes (the first 8 bands form a
    # partition of the boxes).  We check that the boxed_data stream is
    # exhausted and contains no more boxes.
    try:
        next(boxed_data)
        assert 0, "Too many boxes found"
    except StopIteration:
        # We fully expect to get here.
        pass

    # *lenz* contains the lengths of each zone 0 to 7 (the number of
    # valid months in each zone).
    lenz, iord = sort_perm(lenz)
    for zone in range(len(band_in_zone)):
        # Find the longest band that is in the compound zone.
        for j1 in range(bands):
            if iord[j1] in band_in_zone[zone]:
                break
        else:
            # Should be an assertion really.
            raise Exception("No band in compound zone %d." % zone)
        band = iord[j1]
        if lenz[band] == 0:
            print("**** NO DATA FOR ZONE %d" % band)
        wtg = list(wt[band])
        avgg = list(avg[band])
        # Add in the remaining bands, in length order.
        for j in range(j1 + 1, bands):
            band = iord[j]
            if band not in band_in_zone[zone]:
                continue
            series.combine(avgg, wtg, avg[band], wt[band], parameters.box_min_overlap)
        series.anomalize(avgg, parameters.box_reference_period, iyrbeg)
        yield (avgg, wtg)


def sort_perm(a):
    """The array *a* is sorted into descending order.  The fresh sorted
    array and the permutation array are returned as a pair (*sorted*,
    *indexes*).  The original *a* is not mutated.

    The *indexes* array is such that `a[indexes[x]] == sorted[x]`.
    """

    z = list(zip(a, range(len(a))))
    z = sorted(z, key=lambda x: x[1] - x[0])
    data, indexes = zip(*z)
    return data, indexes


def zones():
    """Return the parameters of the 16 zones (8 basic bands and 8
    additional compound zones).

    A pair of (*boxes_in_band*,*band_in_zone*) is returned.
    `boxes_in_band[b]` gives the number of boxes in band
    *b* for `b in range(8)`.  *band_in_zone* defines how the 6
    combined zones are made from the basic bands.  `b in
    band_in_zone[k]` is true when basic band *b* is in compound zone
    *z* (*b* is in range(8), *z* is in range(6)).

    Implicit (in this function and its callers) is that 8 basic bands
    form a decomposition of the 80 boxes.  All you need to know is the
    number of boxes in each band; simply take the next N boxes to make
    the next band.
    """

    # Number of boxes (regions) in each band.
    boxes_in_band = [4, 8, 12, 16, 16, 12, 8, 4]

    N = set(range(4))  # Northern hemisphere
    G = set(range(8))  # Global
    S = G - N  # Southern hemisphere
    T = {3, 4}  # Tropics
    NM = {1, 2}  # Northern mid-latitudes
    SM = {5, 6}  # Southern mid-latitudes
    band_in_zone = [N - T, T, S - T, NM, SM, N, S, G]

    return boxes_in_band, band_in_zone


def annzon(meta, zoned_averages, alternate=None):
    if alternate is None:
        alternate = {"global": 2, "hemi": True}
    """Compute annual zoned anomalies. *zoned_averages* is an iterator
    of zoned averages produced by `zonav`.

    The *alternate* argument controls whether alternate algorithms are
    used to compute the global and hemispheric means.
    alternate['global'] is 1 or 2, to select 1 of 2 different
    alternate computations, or false to not compute an alternative;
    alternate['hemi'] is true to compute an alternative, false
    otherwise.
    """

    zones = 16
    monm = meta.monm
    iyrs = monm // 12

    # Allocate the 2- and 3- dimensional arrays.
    # The *data* and *wt* arrays are properly 3 dimensional
    # ([zone][year][month]), but the inner frames are only allocated
    # when we read the data, see :read:zonal below.
    data = [None for _ in range(zones)]
    wt = [None for _ in range(zones)]
    ann = [[MISSING] * iyrs for _ in range(zones)]

    # Collect zonal means.
    for zone in range(zones):
        (tdata, twt) = next(zoned_averages)

        # Regroup the *data* and *wt* series so that they come in blocks of 12.
        # Uses essentially the same trick as the `grouper()` recipe in
        # http://docs.python.org/library/itertools.html#recipes
        data[zone] = list(zip(*[iter(tdata)] * 12))

        wt[zone] = zip(*[iter(twt)] * 12)

    # Find (compute) the annual means.
    for zone in range(zones):
        for iy in range(iyrs):
            anniy = 0.0
            mon = 0
            for m in range(12):
                if data[zone][iy][m] == MISSING:
                    continue
                mon += 1
                anniy += data[zone][iy][m]

            if mon >= parameters.zone_annual_min_months:
                ann[zone][iy] = float(anniy) / mon

    # Alternate global mean.
    if alternate["global"]:
        glb = alternate["global"]
        assert glb in (1, 2)
        # Pick which "four" zones to use.
        # (subtracting 1 from each zone to convert to Python convention)
        if glb == 1:
            zone = [8, 9, 9, 10]
        else:
            zone = [8, 3, 4, 10]
        wtsp = [3.0, 2.0, 2.0, 3.0]
        for iy in range(iyrs):
            glob = 0.0
            ann[-1][iy] = MISSING
            for z, w in zip(zone, wtsp):
                if ann[z][iy] == MISSING:
                    # Note: Rather ugly use of "for...else" to emulate GOTO.
                    break
                glob += ann[z][iy] * w
            else:
                ann[-1][iy] = 0.1 * glob
        for iy in range(iyrs):
            data[-1][iy] = [MISSING] * 12
            for m in range(12):
                glob = 0.0
                for z, w in zip(zone, wtsp):
                    if data[z][iy][m] == MISSING:
                        break
                    glob += data[z][iy][m] * w
                else:
                    data[-1][iy][m] = 0.1 * glob

    # Alternate hemispheric means.
    if alternate["hemi"]:
        # For the computations it will be useful to recall how the zones
        # are numbered.  There is a useful docstring at the beginning of
        # zonav().
        for ihem in range(2):
            for iy in range(iyrs):
                ann[ihem + 11][iy] = MISSING
                if ann[ihem + 3][iy] != MISSING and ann[2 * ihem + 8][iy] != MISSING:
                    ann[ihem + 11][iy] = (
                        0.4 * ann[ihem + 3][iy] + 0.6 * ann[2 * ihem + 8][iy]
                    )
            for iy in range(iyrs):
                data[ihem + 11][iy] = [MISSING] * 12
                for m in range(12):
                    if (
                        data[ihem + 3][iy][m] != MISSING
                        and data[2 * ihem + 8][iy][m] != MISSING
                    ):
                        data[ihem + 11][iy][m] = (
                            0.4 * data[ihem + 3][iy][m]
                            + 0.6 * data[2 * ihem + 8][iy][m]
                        )

    return meta, data, wt, ann, parameters.zone_annual_min_months


def ensure_weight(data):
    """Take a stream of (weight,land,ocean) record triples, if the
    weight stream is None (the usual case in fact), then generate a
    weight by considering the land and ocean records.  A series of
    triples is yielded.

    *weight* will be 1 when the land record is to be used, and 0
    if the ocean record is to be used.
    """

    meta = next(data)
    maskmeta, landmeta, oceanmeta = meta
    if maskmeta:
        yield meta
        for t in data:
            yield t
    else:
        meta = list(meta)
        meta[0] = "mask computed in Step 5"
        yield tuple(meta)
        for _, land, ocean in data:
            if (
                ocean.good_count < parameters.subbox_min_valid
                or land.d < parameters.subbox_land_range
            ):
                landmask = 1.0
            else:
                landmask = 0.0
            yield landmask, land, ocean


def step5(data):
    """Step 5 of GISTEMP.

    This step takes input provided by steps 3 and 4 (zipped together).

    The usual generator of the *data* argument is gio.step5_input()
    and this allows for various missing and/or synthesized inputs,
    allowing just-land, just-ocean, override-weights.

    :Param data:
        *data* should be an iterable of (weight, land, ocean) triples.  The
        first triple is metadata (and this is a hack).  Subsequently
        there is one triple per subbox (of which, 8000).

    """
    subboxes = ensure_weight(data)
    subboxes = gio.step5_mask_output(subboxes)

    # The result of `as_boxes` is a stream of boxes for each of 3
    # separate analyses: land only, land and ocean combined.
    land, mixed = as_boxes(subboxes)

    result = []

    for meta, boxes in [land, mixed]:
        boxes = gio.step5_bx_output(meta, boxes)
        zoned_averages = zonav(meta, boxes)
        result.append(annzon(meta, zoned_averages))
    return result
