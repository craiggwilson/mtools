from mtools.util.logevent import LogEvent
from mtools.util.input_source import InputSource

from math import ceil 
from datetime import datetime
import time
import re

class LogFile(InputSource):
    """ wrapper class for log files, either as open file streams of from stdin. """

    def __init__(self, filehandle):
        """ provide logfile as open file stream or stdin. """
        self.filehandle = filehandle
        self.name = filehandle.name
        
        self.from_stdin = filehandle.name == "<stdin>"
        self._start = None
        self._end = None
        self._filesize = None
        self._num_lines = None
        self._restarts = None
        self._binary = None

        self._datetime_format = None
        self._year_rollover = None

        # make sure bounds are calculated before starting to iterate, including potential year rollovers
        self._calculate_bounds()

    @property
    def start(self):
        """ lazy evaluation of start and end of logfile. Returns None for stdin input currently. """
        if not self._start:
            self._calculate_bounds()
        return self._start

    @property
    def end(self):
        """ lazy evaluation of start and end of logfile. Returns None for stdin input currently. """
        if not self._end:
            self._calculate_bounds()
        return self._end

    @property
    def filesize(self):
        """ lazy evaluation of start and end of logfile. Returns None for stdin input currently. """
        if self.from_stdin:
            return None
        if not self._filesize:
            self._calculate_bounds()
        return self._filesize

    @property
    def datetime_format(self):
        """ lazy evaluation of the datetime format. """
        if not self._datetime_format:
            self._calculate_bounds()
        return self._datetime_format

    @property
    def year_rollover(self):
        """ lazy evaluation of the datetime format. """
        if self._year_rollover == None:
            self._calculate_bounds()
        return self._year_rollover

    @property
    def num_lines(self):
        """ lazy evaluation of the number of lines. Returns None for stdin input currently. """
        if self.from_stdin:
            return None
        if not self._num_lines:
            self._iterate_lines()
        return self._num_lines

    @property
    def restarts(self):
        """ lazy evaluation of all restarts. """
        if not self._num_lines:
            self._iterate_lines()
        return self._restarts

    @property
    def binary(self):
        """ lazy evaluation of the binary name. """
        if not self._num_lines:
            self._iterate_lines()
        return self._binary

    @property
    def versions(self):
        """ return all version changes. """
        versions = []
        for v, _ in self.restarts:
            if len(versions) == 0 or v != versions[-1]:
                versions.append(v)
        return versions

    def next(self):
        """ get next line, adjust for year rollover and hint datetime format. """

        # use readline here because next() iterator uses internal readahead buffer so seek position is wrong
        line = self.filehandle.readline()
        if line == '':
            raise StopIteration
        line = line.rstrip('\n')

        le = LogEvent(line)
        
        # hint format and nextpos from previous line
        if self._datetime_format and self._datetime_nextpos != None:
            ret = le.set_datetime_hint(self._datetime_format, self._datetime_nextpos, self.year_rollover)
            if not ret:
                # logevent indicates timestamp format has changed, invalidate hint info
                self._datetime_format = None
                self._datetime_nextpos = None
        elif le.datetime:
            # print "not hinting"
            # gather new hint info from another logevent
            self._datetime_format = le.datetime_format
            self._datetime_nextpos = le._datetime_nextpos  

        return le

    def __iter__(self):
        """ iteration over LogFile object will return a LogEvent object for each line (generator) """
        le = None
        
        while True:
            try:
                le = self.next()
            except StopIteration as e:
                # end of log file, get end date
                if not self.end and self.from_stdin:
                    if le and le.datetime:
                        self._end = le.datetime

                # future iterations start from the beginning
                if not self.from_stdin:
                    self.filehandle.seek(0)
                
                # now raise StopIteration exception
                raise e

            # get start date for stdin input
            if not self.start and self.from_stdin:
                if le and le.datetime:
                    self._start = le.datetime

            yield le


    def __len__(self):
        """ return the number of lines in a log file. """
        return self.num_lines


    def _iterate_lines(self):
        """ count number of lines (can be expensive). """
        self._num_lines = 0
        self._restarts = []

        l = 0
        for l, line in enumerate(self.filehandle):

            # find version string
            if "version" in line:

                restart = None
                # differentiate between different variations
                if "mongos" in line or "MongoS" in line:
                    self._binary = 'mongos'
                elif "db version v" in line:
                    self._binary = 'mongod'

                else: 
                    continue

                version = re.search(r'(\d\.\d\.\d+)', line)
                if version:
                    version = version.group(1)
                    restart = (version, LogEvent(line))
                    self._restarts.append(restart)

        self._num_lines = l+1

        # reset logfile
        self.filehandle.seek(0)


    def _calculate_bounds(self):
        """ calculate beginning and end of logfile. """

        if self.from_stdin: 
            return False

        # get start datetime 
        for line in self.filehandle:
            logevent = LogEvent(line)
            if logevent.datetime:
                self._start = logevent.datetime
                self._datetime_format = logevent.datetime_format
                self._datetime_nextpos = logevent._datetime_nextpos
                break

        # get end datetime (lines are at most 10k, go back 30k at most to make sure we catch one)
        self.filehandle.seek(0, 2)
        self._filesize = self.filehandle.tell()
        self.filehandle.seek(-min(self._filesize, 30000), 2)

        for line in reversed(self.filehandle.readlines()):
            logevent = LogEvent(line)
            if logevent.datetime:
                self._end = logevent.datetime
                break

        # if there was a roll-over, subtract 1 year from start time
        if self._end < self._start:
            self._start = self._start.replace(year=self._start.year-1)
            self._year_rollover = self._end
        else:
            self._year_rollover = False

        # reset logfile
        self.filehandle.seek(0)

        return True


    def _find_curr_line(self, prev=False):
        """ internal helper function that finds the current (or previous if prev=True) line in a log file
            based on the current seek position.
        """
        curr_pos = self.filehandle.tell()
        line = None

        # jump back 15k characters (at most) and find last newline char
        jump_back = min(self.filehandle.tell(), 15000)
        self.filehandle.seek(-jump_back, 1)
        buff = self.filehandle.read(jump_back)
        self.filehandle.seek(curr_pos, 0)

        newline_pos = buff.rfind('\n')
        if prev:
            newline_pos = buff[:newline_pos].rfind('\n')

        # move back to last newline char
        if newline_pos == -1:
            self.filehandle.seek(0)
            return self.next()

        self.filehandle.seek(newline_pos - jump_back + 1, 1)

        # roll forward until we found a line with a datetime
        try:
            logevent = self.next()
            while not logevent.datetime:
                logevent = self.next()

            return logevent
        except StopIteration:
            # reached end of file
            return None


    def fast_forward(self, start_dt):
        """ Fast-forward a log file to the given start_dt datetime object using binary search.
            Only fast for files. Streams need to be forwarded manually, and it will miss the 
            first line that would otherwise match (as it consumes the log line). 
        """
        if self.from_stdin:
            # skip lines until start_dt is reached
            return

        else:
            # fast bisection path
            min_mark = 0
            max_mark = self.filesize
            step_size = max_mark

            # check if start_dt is already smaller than first datetime
            self.filehandle.seek(0)
            le = self.next()
            if le.datetime and le.datetime >= start_dt:
                self.filehandle.seek(0)
                return

            le = None
            self.filehandle.seek(0)

            # search for lower bound
            while abs(step_size) > 100:
                step_size = ceil(step_size / 2.)
                
                self.filehandle.seek(step_size, 1)
                le = self._find_curr_line()
                if not le:
                    break
                                
                if le.datetime >= start_dt:
                    step_size = -abs(step_size)
                else:
                    step_size = abs(step_size)

            if not le:
                return

            # now walk backwards until we found a truely smaller line
            while le and self.filehandle.tell() >= 2 and le.datetime >= start_dt:
                self.filehandle.seek(-2, 1)
                le = self._find_curr_line(prev=True)



