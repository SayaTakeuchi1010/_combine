#!/usr/bin/env python
"""
Data loader for Rigaku table-top X-ray source .ras file format.

Provides :func:`load` and :func:`loads` functions to load a Rigaku RAS format
file.  These functions return a list of file segments containing the
following::

    sample : sample name
    comment : file comment

    x : x data
    y : scaled counts, not accounting for count time or attenuators
    y_err : count uncertainty for scaled counts

    x_label, x_unit : x axis label and units
    y_label, y_unit : y axis label and units

    x_resolution : uncertainty in x position?
    wavelength : wavelength of the beam (Angstroms)
    wavelength_resolution : 1-sigma wavelength resolution
    slit1_distance : negative distance from sample to selection slit
    slit2_distance : negative distance from sample to incident slit
    slit3_distance : distance from sample to receiving slit 1
    slit4_distance : distance from sample to receiving slit 2

    scan_mode : STEP | ?
    scan_axis : which motors are being scanned
        TwoThetaOmega, TwoThetaTheta
            sample angle = x/2 + Omega - TwoTheta/2
            detector angle = x
        Omega
            sample angle = x
            detector angle = TwoTheta
        TwoTheta
            sample angle = Omega
            detector angle = x

    count_time, count_time_unit : counting time and units for the segment
    start_time, end_time : start and end time for segment, as struct_time

    axis : { name: [label, unit, position, offset] } instrument configuration,
    including the following fields

        TwoTheta, Omega
        Chi, Phi
        Z, Rx, Ry
        TwoThetaTheta, TwoThetaOmega, TwoThetaChi, TwoThetaChiPhi
        Alpha, Beta
        ThetaS, ThetaD
        Ts, Zs
        CBO, CBO-M
        Incident{SollerSlit, AxdSlit, SlitBox{,-_Axis}, Monochromator{,-OmegaM}}
        CenterSlit, Filter, Attenuator
        Receiving{SollerSlit, AxdSlit, SlitBox[12]{,-_Axis}, Optics, SlitBox2-Zd}
        Counter{Monochromator, Slit}
        TwoThetaB, AlphaR, BetaR
        IncidintPrimary, HV, PHA

    full_header : all the Rigaku header fields as string float or int values

The CBO axis (Cross Beam Optics) can be 'BB' for Bragg-Brentano divergent
beam optics or 'PB' for parallel beam optics.  Convergent beam optics
requires an elliptical mirror rather than the parabolic mirror used for the
parallel beam optics. Likely this will be indicated by the presence
of the 'CBO-E unit' as one of the 'HW_I_OPT_NAME-#' fields.  Perhaps the CBO
axis will use the code 'CB' instead of 'PB' for this case?

Slit distances are not stored in the file, and instead came from the schematic
from the Rigaku SmartLab instrument.

Wavelength is determined by target and monochromator, with the file containing
values for K_a1, K_a2 and K_b.  With the Ge(220)x2 monochromator, it is possible
to select just 'Ka1', as indicated by the value in 'MEAS_COND_XG_WAVE_TYPE'.
K_alpha linewidths are tiny[1], mostly less than 0.05% FWHM.  This value is
used to set the wavelength error independent of target 'HW_XG_TARGET_NAME'.
For high intensity measurements use (2*Ka1 + Ka2)/3 for the wavelength, and
standard deviation of (Ka1, Ka1, Ka2) for the 1-sigma dispersion.  Note: do not
yet know how this is indicated in the file.

[1] https://wwwastro.msfc.nasa.gov/xraycal/linewidths.html
"""

from __future__ import division, print_function
import warnings
import sys
import traceback
import logging
from time import strptime

import numpy as np

if sys.version_info[0] >= 3:
    def tostr(s):
        return s.decode('ascii')
else:
    def tostr(s):
        return s

# Time format in Rigaku .ras files is "mm/dd/yy HH:MM:SS"
TIME_FORMAT = "%m/%d/%y %H:%M:%S"

# Copied from anno_exc so that rigaku.py is stand-alone
def annotate_exception(msg, exc=None):
    """
    Add an annotation to the current exception, which can then be forwarded
    to the caller using a bare "raise" statement to reraise the annotated
    exception.
    """
    if not exc:
        exc = sys.exc_info()[1]

    args = exc.args
    if not args:
        arg0 = msg
    else:
        arg0 = " ".join((str(args[0]), msg))
    exc.args = tuple([arg0] + list(args[1:]))

def load(filename):
    """
    Load Rigaku data from file.
    """
    with open(filename, 'rb') as fid:
        data = fid.read()
    try:
        return loads(data)
    except Exception as exc:
        #traceback.print_exc()
        annotate_exception("while loading %r"%filename)
        raise

def loads(data):
    """
    Load Rigaku data from string.
    """
    lines = data.split(b'\r\n')
    if lines[0] != b"*RAS_DATA_START":
        raise ValueError("not a Rigaku XRD RAS file")
    index = 1
    datasets = []
    while True:
        index, header, values = _parse(index, lines)
        data = _interpret(header, values)
        datasets.append(data)
        if lines[index] == b"*RAS_DATA_END":
            break
    return datasets

def join(datasets):
    """
    Combine Rigaku data segments into a single scan.
    """
    first = datasets[0]
    if any(data['scan_axis'] != first['scan_axis']
           for data in datasets):
        raise ValueError("Can't mix different scan types")
    if any(data['axis']['Attenuator'][2] != first['axis']['Attenuator'][2]
           for data in datasets):
        raise ValueError("Can't mix different attenuation values")

    R = first.copy()
    R['x'] = np.hstack([data['x'] for data in datasets])
    R['y'] = np.hstack([data['y'] for data in datasets])
    R['y_err'] = np.hstack([data['y_err'] for data in datasets])
    R['count_time'] = np.hstack([data['count_time']*np.ones_like(data['x'])
                                 for data in datasets])
    R['start_time'] = min(data['start_time'] for data in datasets)
    R['end_time'] = max(data['end_time'] for data in datasets)
    return R

def _parse(index, lines):
    if lines[index] != b"*RAS_HEADER_START":
        raise ValueError("corrupt file: missing *RAS_HEADER_START")
    index += 1
    header = {}
    while True:
        if index >= len(lines):
            raise ValueError("corrupt file: missing *RAS_HEADER_END")
        line = lines[index]
        index += 1
        if line == b'*RAS_HEADER_END':
            break
        #print(index, ":", line)
        try:
            key, value = line.split(b' ', 1)  # *KEY "value"
            # Note: py3 byte strings key[k] returns ord not byte string
            assert key[0:1] == b"*"
            assert value[0:1] == b'"' and value[-1:] == b'"'
        except Exception:
            #traceback.print_exc()
            raise ValueError("corrupt file: line %d is not '*KEY value'"%index)
        key = tostr(key[1:])
        value = value[1:-1] # strip quotes

        # auto convert values to int or float if possible
        # for string values, try splitting "japanese?|english" to english
        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                try:
                    _, value = value.split(b'|')
                except ValueError:
                    pass
                value = tostr(value)
        # if all conversions fail, value should be an untouched string

        header[key] = value

    if lines[index] != b"*RAS_INT_START":
        raise ValueError("corrupt file: missing *RAS_INT_START")
    index += 1
    values = []
    while True:
        if index >= len(lines):
            raise ValueError("corrupt file: missing *RAS_INT_END")
        line = lines[index]
        index += 1
        if line == b"*RAS_INT_END":
            break
        try:
            values.append([float(v) for v in line.split()])
        except Exception:
            raise ValueError("corrupt file: line %d is not a set of values"%index)

    return index, header, values

# From https://wwwastro.msfc.nasa.gov/xraycal/linewidths.html
EMISSION_LINEWIDTH = 0.00021  # 0.05% dL/L FWHM as 1-sigma line width

# From Rigaku manual, pg 17, pg 131
MONOCHROMATOR_WAVELENGTH_RESOLUTION = {
    # 'mirror': Ka1 + Ka2 + Kb
    'Ge(220)x2': 3.8e-4,
    'Ge(400)x2': np.NaN,  # Manual doesn't list the resolution for this config.
    'Ge(220)x4': 1.5e-4,
    'Ge(440)x4': 2.3e-5,
}

# From Rigaku manual, pg 131, converted from seconds of arc
MONOCHROMATOR_ANGULAR_DIVERGENCE = {
    'mirror': 4.1e-2,
    'Ge(220)x2': 8.8e-3,
    'Ge(400)x2': 1.2e-2,
    'Ge(220)x4': 3.4e-3,
    'Ge(440)x4': 1.5e-3,
}
# Note: receiving resolution (pg 17) is a factor of three lower than expected
# from the equation 1/2 (s1 + s2) / |d1 - d2|.  If the manual is giving values
# as 1-sigma, then they are 25% lower than expected.
def _interpret(header, values):
    R = {}
    x, I, scale = np.array(values).T
    R['x'] = x
    R['y'] = I*scale
    R['y_err'] = np.sqrt(I)*scale

    R['x_label'] = header['MEAS_SCAN_AXIS_X']
    R['x_unit'] = header['MEAS_SCAN_UNIT_X']
    R['x_resolution'] = header['MEAS_SCAN_RESOLUTION_X']
    R['y_label'] = header['DISP_TITLE_Y']
    R['y_unit'] = header['MEAS_SCAN_UNIT_Y']

    R['count_time'] = header['MEAS_SCAN_SPEED']
    R['count_time_unit'] = header['MEAS_SCAN_SPEED_UNIT']

    R['sample'] = header['FILE_SAMPLE']
    R['comment'] = header['FILE_COMMENT']
    R['start_time'] = strptime(header['MEAS_SCAN_START_TIME'], TIME_FORMAT)
    R['end_time'] = strptime(header['MEAS_SCAN_END_TIME'], TIME_FORMAT)
    R['axis'] = _interpret_axes(header)
    R['scan_axis'] = header['MEAS_SCAN_AXIS_X_INTERNAL']
    R['scan_mode'] = header['MEAS_SCAN_MODE']
    #R['scan_steps'] = (header['MEAS_SCAN_START'], header['MEAS_SCAN_STEP'],
    #                   header['MEAS_SCAN_STOP'])

    R['slit1_distance'] = 114.-300.  # mm  Selection slit
    R['slit2_distance'] = 190.-300.  # mm  Incident slit
    R['slit3_distance'] = 187.  # mm  Receiving slit 1
    R['slit4_distance'] = 300.  # mm  Receiving slit 2

    monochromator = R['axis']['IncidentMonochromator'][2]
    #print("monochromator", monochromator)
    R['wavelength_resolution'] = MONOCHROMATOR_WAVELENGTH_RESOLUTION.get(monochromator, np.NaN)
    R['angular_divergence'] = MONOCHROMATOR_ANGULAR_DIVERGENCE.get(monochromator, np.NaN)
    if header['MEAS_COND_XG_WAVE_TYPE'] == "Ka1":
        R['wavelength'] = header['HW_XG_WAVE_LENGTH_ALPHA1']
    elif header['MEAS_COND_XG_WAVE_TYPE'] == "Ka2":
        R['wavelength'] = header['HW_XG_WAVE_LENGTH_ALPHA1']
    elif header['MEAS_COND_XG_WAVE_TYPE'] == "Kb":
        R['wavelength'] = header['HW_XG_WAVE_LENGTH_BETA']
    else:
        # TODO: missing K_beta contribution
        Ka1 = header['HW_XG_WAVE_LENGTH_ALPHA1']
        Ka2 = header['HW_XG_WAVE_LENGTH_ALPHA2']
        Kbeta = header['HW_XG_WAVE_LENGTH_BETA']
        R['wavelength'] = (2*Ka1 + Ka2)/3
        R['wavelength_resolution'] = np.std([Ka1, Ka1, Ka2], ddof=1)

    R['full_header'] = header
    return R

def _interpret_axes(header):
    axis = {}
    idx = 0
    while 'MEAS_COND_AXIS_NAME-%d'%idx in header:
        # Axis properties as string values direct from the header
        label = header['MEAS_COND_AXIS_NAME-%d'%idx]
        unit = header['MEAS_COND_AXIS_UNIT-%d'%idx]
        name = header['MEAS_COND_AXIS_NAME_INTERNAL-%d'%idx]
        magicno = header['MEAS_COND_AXIS_NAME_MAGICNO-%d'%idx]
        offset = header['MEAS_COND_AXIS_OFFSET-%d'%idx]
        position = header['MEAS_COND_AXIS_POSITION-%d'%idx]

        # Convert position to float if possible.  Note that there are
        # non-float values for positions, such as "1/10000" for attenuators
        # and "0.2mm" for slit boxes.  There are also string names
        # such "PSA_open" for the receiving optical device field.
        if name == "Attenuator":
            # Attenuator position may be represented as a fraction, such as 1/10000.
            # Not how attenuator position is represented if absent, so I'm
            # testing a bunch of different possible strings.  Or maybe it is a
            # numeric value such as "1".
            if "/" in position:
                numerator, denominator = position.split('/')
                position = float(numerator)/float(denominator)
            elif position in ("", "-", "None"):
                position = 1.0
            # Maybe test for zero, or maybe not.  It is possible that the
            # attenuation factor is set to zero when there is a beam stop
            # in place for dark beam measurements.  Leave alone for now
            # until we have a dataset which has attenuator==0.
            #if position == 0.: position = 1.
        elif not isinstance(position, (int, float)) and position.endswith('mm'):
            unit = 'mm'
            position = float(position[:-2])
        elif position == "None":
            position = None
        elif position == "-":
            position = np.NaN

        # Offsets are much easier: either "-" or a floating point number
        if offset == "-":
            offset = np.NaN

        axis[name] = label, unit, position, offset
        idx += 1
    return axis

def plot(datasets, joined=True, label=None):
    from matplotlib import pyplot as plt
    if joined:
        R = join(datasets)
        plt.errorbar(R['x'], R['y']/R['count_time'], yerr=R['y_err']/R['count_time'],
                     fmt='.', label=label)
        plt.xlabel("%s (%s)" % (R['x_label'], R['x_unit']))
        plt.ylabel("%s (%s/%s)" % (R['y_label'], R['y_unit'], R['count_time_unit']))
    else:
        for k, data in enumerate(datasets):
            attenuator = data['axis']['Attenuator'][2]
            count_time = data['count_time']
            scale = 1/(attenuator*count_time)
            plt.errorbar(data['x'], data['y']*scale, yerr=data['y_err']*scale,
                         fmt='.', label=(label + "-" + str(k) if label else label))
        plt.xlabel("%s (%s)" % (data['x_label'], data['x_unit']))
        plt.ylabel("%s (%s/%s)" % (data['y_label'], data['y_unit'], data['count_time_unit']))
    plt.yscale('log')
    plt.legend()

def show_header_diff(datasets):
    keys = set.union(*(set(data['full_header'].keys()) for data in datasets))
    values = {k: [] for k in keys}
    for data in datasets:
        for k in keys:
            values[k].append(data['full_header'].get(k, None))
    for k, v in sorted(values.items()):
        if any(vk != v[0] for vk in v):
            print(k, v)

def main_pprint():
    from pprint import pprint
    pprint(load(sys.argv[1]))

def main_plot():
    from matplotlib import pyplot as plt
    for filename in sys.argv[1:]:
        datasets = load(filename)
        plot(datasets, joined=len(sys.argv) > 2, label=filename)
    plt.show()

def main_headers():
    from pprint import pprint
    show_header_table(load(sys.argv[1]))

if __name__ == "__main__":
    #main_pprint()
    main_plot()
    #main_headers()