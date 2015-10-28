# This program is public domain

"""
Load a NeXus file into a reflectometry data structure.
"""
import os
import tempfile
from zipfile import ZipFile

import numpy as np
import h5py as h5

from .. import refldata
from .. import corrections as cor
from .. import unit
from .. import iso8601

def data_as(group, fieldname, units, rep=1):
    """
    Return value of field in the desired units.
    """
    if fieldname not in group:
        return np.NaN
    field = group[fieldname]
    converter = unit.Converter(field.attrs.get('units', ''))
    value = converter(field.value, units)
    if rep != 1:
        if value.shape[0] == 1:
            return np.repeat(value, rep, axis=0)
        elif value.shape[0] != rep:
            raise ValueError("field %r does not match counts in %r"
                             %(field.name,field.file.filename))
        else:
            return value
    else:
        return value

def nxfind(group, nxclass):
    """
    Iterate over the entries of type *nxclass* in the hdf5 *group*.
    """
    for entry in group.values():
        if nxclass == entry.attrs.get('NX_class', None):
            yield entry

def load_entries(filename):
    """
    Load the summary info for all entries in a NeXus file.
    """
    #file = h5.File(filename)
    file = h5_open_zip(filename)
    file._opened_entries = 0  # keep track of the number of entries open
    measurements = []
    for name,entry in file.items():
        if entry.attrs.get('NX_class', None) == 'NXentry':
            measurements.append(NCNRNeXusRefl(entry, name, filename))
            file._opened_entries += 1
    return measurements


def h5_open_zip(filename, mode='r', **kw):
    """
    Open a NeXus file, even if it is in a zip file.

    If the filename ends in '.zip', it will be unzipped to a temporary
    directory before opening and deleted on :func:`closezip`.  If opened
    for writing, then the file will be created in a temporary directory,
    then zipped and deleted on :func:`closezip`.

    Arguments are the same as for :func:`open`.
    """
    is_zip = filename.endswith('.zip')
    zip_on_close = None
    if is_zip:
        path = tempfile.gettempdir()
        if mode == 'r':
            zf = ZipFile(filename)
            members = zf.namelist()
            assert len(members) == 1
            zf.extract(members[0], path)
            filename = os.path.join(path, members[0])
        elif mode == 'w':
            zip_on_close = filename
            filename = os.path.join(path, os.path.basename(filename)[:-4])
        else:
            raise TypeError("zipped nexus files only support mode r and w")

    f = h5.File(filename, mode=mode, **kw)
    f.delete_on_close = is_zip
    f.zip_on_close = zip_on_close
    return f

def h5_close_zip(f):
    """
    Close a NeXus file opened by :func:`open_zip`.

    If the file was zipped and opened for reading, delete the temporary
    file that was created
    If opened for writing, create a zip file containing the file
    before closing.

    Delete the file after closing.
    """
    path = f.filename
    delete_on_close = getattr(f, 'delete_on_close', False)
    zip_on_close = getattr(f, 'zip_on_close', None)
    f.close()
    if zip_on_close is not None:
        with ZipFile(f.zip_on_close, 'w') as zf:
            zf.write(path, os.path.basename(path))
    if delete_on_close:
        os.unlink(path)


class NCNRNeXusRefl(refldata.ReflData):
    """
    NeXus reflectometry entry.

    See :class:`reflred.refldata.ReflData` for details.
    """
    format = "NeXus"

    def __init__(self, entry, entryname, filename):
        super(NCNRNeXusRefl,self).__init__()
        self.entry = entryname
        self.filename = os.path.abspath(filename)
        self.name = os.path.basename(filename).split('.')[0]
        self._set_metadata(entry)

    def _set_metadata(self, entry):
        # TODO: ought to close file when we are done loading, which means
        # we shouldn't hold on to entry
        print("Don't forget to close the hdf file!!")
        self._entry = entry
        #print(entry['instrument'].values())
        das = self._entry['DAS_logs']
        self.probe = 'neutron'
        self.date = iso8601.parse_date(entry['start_time'][0])
        self.description = entry['experiment_description'][0]
        self.instrument = entry['instrument/name'][0]
        self.slit1.distance = data_as(entry,'instrument/presample_slit1/distance','mm')
        self.slit2.distance = data_as(entry,'instrument/presample_slit2/distance','mm')
        #self.slit3.distance = data_as(entry,'instrument/predetector_slit1/distance','mm')
        #self.slit4.distance = data_as(entry,'instrument/predetector_slit2/distance','mm')
        #self.detector.distance = data_as(entry,'instrument/detector/distance','mm')
        #self.detector.rotation = data_as(entry,'instrument/detector/rotation','degree')
        self.detector.wavelength = data_as(entry,'instrument/monochromator/wavelength','Ang')
        self.detector.wavelength_resolution = data_as(entry,'instrument/monochromator/wavelength_error','Ang')

        self.sample.description = entry['sample/description'][0]
        self.monitor.base = das['counter/countAgainst'][0]
        self.monitor.time_step = 0.001  # assume 1 ms accuracy on reported clock
        self.polarization = _get_pol(das, 'frontPolarization') \
                            + _get_pol(das, 'backPolarization')

        if np.isnan(self.slit1.distance):
            self.warn("Slit 1 distance is missing; using 2 m")
            self.slit1.distance = -2000
        if np.isnan(self.slit2.distance):
            self.warn("Slit 2 distance is missing; using 1 m")
            self.slit2.distance = -1000
        if np.isnan(self.detector.wavelength):
            self.warn("Wavelength is missing; using 4.75 A")
            self.detector.wavelength = 4.75
        if np.isnan(self.detector.wavelength_resolution):
            self.warn("Wavelength resolution is missing; using 2% dL/L FWHM")
            self.detector.wavelength_resolution = 0.02*self.detector.wavelength

    def load(self):
        das = self._entry['DAS_logs']
        self.detector.counts = np.asarray(das['pointDetector/counts'][:,0], 'd')
        self.detector.dims = self.detector.counts.shape
        n = self.detector.dims[0]
        self.monitor.counts = np.asarray(data_as(das,'counter/liveMonitor','',rep=n), 'd')
        self.monitor.count_time = data_as(das,'counter/liveTime','s',rep=n)
        self.slit1.x = data_as(das,'slitAperture1/softPosition','mm',rep=n)
        self.slit2.x = data_as(das,'slitAperture2/softPosition','mm',rep=n)
        self.slit3.x = data_as(das,'slitAperture3/softPosition','mm',rep=n)
        self.slit4.x = data_as(das,'slitAperture4/softPosition','mm',rep=n)
        self.sample.angle_x = data_as(das,'sampleAngle/softPosition','degree',rep=n)
        self.detector.angle_x = data_as(das,'detectorAngle/softPosition','degree',rep=n)
        #TODO: temperature, field
        if '_theta_offset' in das['trajectoryData']:
            self.background_offset = 'theta'

        try:
            cor.apply_standard_corrections(self)
        except:
            print self.filename
            import traceback; traceback.print_exc()


    def _load_slits(self, instrument):
        """
        Slit names have not been standardized.  Instead sort the
        NXaperature components by distance and assign them according
        to serial order, negative aperatures first and positive second.
        """
        raise NotImplementedError("hardcode slit names for now")
        slits = list(nxfind(instrument, 'NXaperature'))
        # Note: only supports to aperatures before and after.
        # Assumes x and y aperatures are coupled in the same
        # component.  This will likely be wrong for some instruments,
        # but we won't deal with that until we have real NeXus files
        # to support.
        # Assume the file writer was sane and specified all slit
        # distances in the same units so that sorting is simple.
        # Currently only slit distance is recorded, not slit opening.
        slits.sort(lambda a,b: -1 if a.distance < b.distance else 1)
        index = 0
        for slit in slits:
            d = slit.distance.value('meters')
            if d <= 0:
                # process first two slits only
                if index == 0:
                    self.slit1.distance = d
                    index += 1
                elif index == 1:
                    self.slit2.distance = d
                    index += 1
            elif d > 0:
                # skip leading slits
                if index < 2: index = 2
                if index == 2:
                    self.slit3.distance = d
                    index += 1
                elif index == 3:
                    self.slit4.distance = d
                    index += 1

def _get_pol(das, pol):
    if pol in das:
        direction = das[pol+'/direction'][0]
        if direction == 'UP':
            result = '+'
        elif direction == 'DOWN':
            result = '-'
        elif direction == '':
            result = ''
        else:
            raise ValueError("Don't understand DAS_logs/%s/direction=%r"%(pol,direction))
    else:
        result = ''
    return result



def demo():
    import sys
    import pylab
    if len(sys.argv) == 1:
        print("usage: python -m reflred.formats.nexusref file...")
        sys.exit(1)
    for f in sys.argv[1:]:
        entries = load_entries(f)
        for f in entries:
            f.load()
            print f
            f.plot()
    pylab.legend()
    pylab.show()

if __name__ == "__main__":
    demo()
