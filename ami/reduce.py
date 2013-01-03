"""
Provides a pythonic wrapper about the AMI reduce pipeline.

(Uses an instance of the interactive `reduce` program driven via pexpect.)

NB It may be possible to do this more directly by writing python wrappers about
the underlying fortran code, but this is a reasonably good quick solution.
"""

## NB I have adopted the convention that each function leaves the 
## spawned instance in a 'ready to receive' state.
## So the last call should usually be to 'expect(blah)'

## Calls to ami often finish with a '\' (backslash), 
# which means 'use defaults'. 
# Unfortunately, this is also the python string escape character.
# So I often use raw strings to make it clear what is being sent. 

import os
import shutil
import pexpect
from environments import ami_env
from collections import defaultdict, namedtuple
import logging
import astropysics.coords
import warnings

SimpleCoords = namedtuple('SimpleCoords', 'ra dec')

from keys import Keys

reduce_logger_name = 'ami.reduce'

def ensure_dir(dirname):
    if not os.path.isdir(dirname):
        os.makedirs(dirname)


class Reduce(object):
    """Class to provide an interface to AMI-reduce package"""
    prompt = 'AMI-reduce>'

    def __init__(self,
                 ami_rootdir,
                 array='LA',
                 log=True,
                 logdir=None,
                 working_dir=None
                 ):
        if working_dir is None:
            working_dir = ami_rootdir
        self.working_dir = working_dir
        self.child = pexpect.spawn('tcsh -c reduce',
                          cwd=self.working_dir,
                          env=ami_env(ami_rootdir))
        self.child.expect(self.prompt)
        #Records all known information about the fileset,
        #Each file entry is initially a ``defaultdict(lambda : None)``
        self.files = dict()
        #Used for updating the relevant record in self.files, also logging:
        self.active_file = None

        if array == 'LA':
            self.switch_to_large_array()
        elif array != 'SA':
            raise ValueError("Initialisation error: Array must be 'LA' or 'SA'.")
        self.array = array

        self.update_files()
        if log:
            self.logdir = logdir
            if self.logdir is None:
                self.logdir = ''
            self.logger = logging.getLogger(reduce_logger_name)
            self.file_log = None
            self.file_cmd_log = None

        else:
            self.logger = None

    def switch_to_large_array(self):
        """NB resets file list"""
        p = self.child
        p.sendline('set def la')
        self.files = dict()
        p.expect(self.prompt)

    def update_files(self):
        p = self.child
        p.sendline(r'list files \ ')
#        p.sendline(r'list comment \ ')
        p.expect(self.prompt)
        #First line in 'before' is command.
        #second line is blank
        #last 4 lines are blanks and 'total obs time'
        file_lines = p.before.split('\n')[2:-4]
        for l in file_lines:
            l = l.strip('\r').strip(' ')
            cols = l.split(' ', 1)
            fname = cols[0]
            if fname not in self.files:
                self.files[fname] = defaultdict(lambda : None)

        p.sendline(r'list comment \ ')
#        p.sendline(r'list comment \ ')
        p.expect(self.prompt)
        file_lines = p.before.split('\n')[2:]
        for l in file_lines:
            l = l.strip('\r').strip(' ')
            cols = l.split(' ', 1)
            fname = cols[0]
            if fname in self.files:
                if len(cols) > 1:
                    self.files[fname][Keys.comment] = cols[1]

    def get_obs_details(self, filename):
        p = self.child
        p.sendline(r'list observation {0} \ '.format(filename))
        p.expect(self.prompt)
        obs_lines = p.before.split('\n')[2:]
        info = self.files[filename]
        info[Keys.pointing] = Reduce._parse_coords(filename, obs_lines)
        info[Keys.calibrator] = Reduce._parse_calibrator(obs_lines)
        return info

    @staticmethod
    def _parse_calibrator(obs_listing):
        for line in obs_listing:
            if 'with calibrator' in line:
                tokens = line.split()
                return tokens[-1]

    @staticmethod
    def _parse_coords(filename, obs_listing):
        for line in obs_listing:
            if 'Tracking' in line:
                if not 'J2000' in line:
                    logging.warn("Obs pointing may not be in J2000 format:"
                         + filename + ", co-ord conversion may be incorrect.")

                coords_str = line[len('Tracking    : '):]
                coords_str = coords_str.strip()
                coords_str = coords_str[:-len('J2000')].strip()
                #Two cases depending whether declination is +ve or -ve:
                if '-' in coords_str:
                    ra_dec = coords_str.split('  ')
                else:
                    ra_dec = coords_str.split('   ')
                pointing = SimpleCoords(ra_dec[0], ra_dec[1])
                return pointing
        raise ValueError("Parsing error for file: %s, coords not found"
                            % filename)

    @staticmethod
    def _convert_to_ap_FK5_coords(simplecoords):
        ra = astropysics.coords.AngularCoordinate(
              simplecoords.ra.replace(' ', ':'), sghms=True)
        dec = astropysics.coords.AngularCoordinate(
              simplecoords.dec.replace(' ', ':'), sghms=False)

        return astropysics.coords.FK5Coordinates(ra, dec)


    def group_pointings(self, pointing_tolerance_in_degrees=0.5):
        """
        Attempt to group together datasets by inspecting pointing target.

        Returns:
        Nested dict with structure:
        { FIRST_FILENAME_IN_GROUP:
            {
            files: [ <list of files>],
            pointing: <string representation of group pointing>
            },
            ...
        }
        """
        group_pointings = defaultdict(list) #Dict, pointing --> Files
        tolerance_deg = pointing_tolerance_in_degrees

        for filename, info in self.files.iteritems():
            if info[Keys.pointing] is None:
                self.get_obs_details(filename)

        for f, info in self.files.iteritems():
            file_pointing = info[Keys.pointing]
            matched = False
            for gp in group_pointings.iterkeys():
                # Unfortunately, FK5 class doesn't serialize well
                # So we only use them as tempvars for easy comparison,
                # Rather than storing in the datadump.
                p0 = Reduce._convert_to_ap_FK5_coords(gp)
                p1 = Reduce._convert_to_ap_FK5_coords(file_pointing)
                if (p0 - p1).degrees < tolerance_deg:
                    group_pointings[gp].append(f)
                    matched = True
#                    print "MATCH", f
#                    print group_pointings[gp]

            if matched is False:
                group_pointings[file_pointing].append(f)
#                print "NEW GROUP", f
#                print group_pointings[file_pointing]

        #Generally the filenames / target names are more recognisable than 
        #plain co-ords
        #So we rename each group by the first (alphabetical) filename,
        #Which should be a target name.
        # (After splitting off the date suffix.)
        named_groups = {}
        for p, files in group_pointings.iteritems():
            name = sorted(files)[0].split('-')[0]
            named_groups[name] = {}
            named_groups[name][Keys.files] = files
            named_groups[name][Keys.pointing] = p

        for grpname, grp_info in named_groups.iteritems():
            for f in grp_info[Keys.files]:
                self.files[f][Keys.group_name] = grpname
        return named_groups

    def _setup_file_loggers(self, filename, file_logdir):
        if (self.logger is not None) or (file_logdir is not None):
            if file_logdir is None:
                file_logdir = self.logdir

            ensure_dir(file_logdir)
            name = os.path.splitext(filename)[0]
            self.file_log = logging.getLogger('.'.join((reduce_logger_name, name)))
#            self.file_log.setLevel(logging.DEBUG)
            fh = logging.FileHandler(
                         os.path.join(file_logdir, name + '.ami.log'),
                         mode='w')
            self.file_log.addHandler(fh)

            self.file_cmd_log = logging.getLogger(
                                  '.'.join((reduce_logger_name, 'commands', name)))

#            self.file_cmd_log.setLevel(logging.DEBUG)
            fh = logging.FileHandler(
                         os.path.join(file_logdir, name + '.ami.commands'),
                         mode='w')
            self.file_cmd_log.addHandler(fh)

    def run_command(self, command):
        self.file_cmd_log.debug(command)
        self.child.sendline(command)
        self.child.expect(self.prompt)
        self.file_log.debug('%s%s', self.prompt, self.child.before)
        self._parse_command_output(command, self.child.before.split('\n'))
        return self.child.before.split('\n')

    def _parse_command_output(self, command, output_lines):
#        try:
        file_info = self.files[self.active_file]
        if 'rain' in command:
            rain_amp_corr = self._parse_rain_results(output_lines)
            file_info[Keys.rain] = rain_amp_corr
            self.logger.info("Rain mean amplitude correction factor: %s",
                             rain_amp_corr)
        if 'flag' in command:
            flagging = self._parse_flagging_results(output_lines)
            file_info[Keys.flagged_max] = max(flagging,
                                                  file_info[Keys.flagged_max])

        if 'reweight' in command:
            est_noise = self._parse_reweight_results(output_lines)
            file_info[Keys.est_noise] = est_noise
            self.logger.info("Estimated noise: %s mJy", est_noise * 1000.0)
                #self.files[self.active_file][Keys.flagging_max]

#        except Exception as e:
#            raise ValueError("Problem parsing command output for file: %s,",
#                             "command: %s, error message:\n%s"
#                             ,self.active_file, command, e.msg)


    def _parse_rain_results(self, output_lines):
        for line in output_lines:
            if "Mean amplitude correction factor" in line:
                return float(line.strip().split()[-1])
        raise ValueError("Parsing error, could not find rain modulation.")

    def _parse_flagging_results(self, output_lines):
        for line in output_lines:
            if "samples flagged" in line:
                if "Total of" in line:
                    tokens = line.strip().split()
                    for t in tokens:
                        if '%' in t:
                            return float(t.strip('%'))

    def _parse_reweight_results(self, output_lines):
        for line in output_lines:
            if "estimated noise" in line:
                tokens = line.strip().split()
                return float(tokens[-2])
        raise ValueError("Parsing error, could not find noise estimate.")

    def run_script(self, script_string):
        """Takes a script of commands, one command per line"""
        command_list = script_string.split('\n')
        for command in command_list:
            self.run_command(command)


    def set_active_file(self, filename, file_logdir=None):
        filename = filename.strip() #Ensure no stray whitespace
        self.logger.info('Active file: %s', filename)
        self.active_file = filename
        self._setup_file_loggers(filename, file_logdir)
        self.run_command(r'file %s \ ' % filename)
        self.get_obs_details(filename)


    def write_files(self, rawfile, output_dir):
        """Writes out UVFITs files.

        NB: You should use this rather than performing writes manually:
        ``reduce`` cannot handle long file paths,
        so rather than cripple the scripting functionality,
        this function hacks around the limitations.
        Kludgey but effective.
        """
        ensure_dir(output_dir)
        tgt_name = os.path.splitext(rawfile)[0]
        tgt_path = os.path.join(output_dir, tgt_name + '.fits')
        cal_basename = (self.files[rawfile][Keys.calibrator] + '-' +
                        tgt_name.split('-')[-1] + 'C.fits')
        cal_path = os.path.join(output_dir, cal_basename)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tgt_temp = os.tempnam(self.working_dir, 'ami_') + '.fits'
            cal_temp = os.tempnam(self.working_dir, 'ami_') + '.fits'

        self.run_command(r'write fits no no all 3-8 all %s %s \ ' %
                         (os.path.basename(tgt_temp),
                          os.path.basename(cal_temp)))

        self.logger.debug("Renaming tempfile %s -> %s", tgt_temp, tgt_path)
        shutil.move(tgt_temp, tgt_path)
        self.logger.debug("Renaming tempfile %s -> %s", cal_temp, cal_path)
        shutil.move(cal_temp, cal_path)
        self.logger.info("Wrote target, calib. UVFITs to:\n\t%s\n\t%s",
                         tgt_path, cal_path)
        info = self.files[self.active_file]
        info[Keys.target_uvfits] = os.path.abspath(tgt_path)
        info[Keys.cal_uvfits] = os.path.abspath(cal_path)

    def update_flagging_info(self):
        lines = self.run_command(r'show flagging no yes \ ')
        final_flagging = self._parse_flagging_results(lines)
        self.files[self.active_file][Keys.flagged_final] = final_flagging
        self.logger.info("Final flagging estimate: %s%%", final_flagging)





