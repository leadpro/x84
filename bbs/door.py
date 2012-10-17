"""
ansiwin package for x/84 BBS http://github.com/jquast/x84
"""
import multiprocessing
import termios
import select
import logging
import struct
import fcntl
import pty
import sys
import re
import os

from bbs.session import getsession
import bbs.exception
import bbs.output
import bbs.cp437
import bbs.ini

PATTERN_EIO = re.compile('Errno 5')
logger = multiprocessing.get_logger()
logger.setLevel(logging.DEBUG)

class Door(object):
    """
    Spawns a subprocess and pipes input and output over bbs session.
    """
    #pylint: disable=R0902,R0903
    #        Too many instance attributes (8/7)
    #        Too few public methods (1/2)
    time_ipoll = 0.05
    time_opoll = 0.05
    blocksize = 7680
    timeout = 1984
    master_fd = None
    decode_cp437 = False
    _TAP = False # for debugging

    def __init__(self, cmd='/bin/uname', args=(), lang=u'en_US.UTF-8',
            term=None, path=None):
        """
        cmd, args = argv[0], argv[1:]
        lang, term, and path become LANG, TERM, and PATH environment
        variables. when term is None, the session terminal type is used.
        When path is None, the .ini 'path' value of section [door] is used.
        """
        #pylint: disable=R0913
        #        Too many arguments (7/5)
        self.cmd = cmd
        self.args = (self.cmd,) + args
        self.lang = lang
        if term is None:
            self.term = getsession().env.get('TERM')
        else:
            self.term = term
        if path is None:
            self.path = bbs.ini.cfg.get('door', 'path')
        else:
            self.path = path

    def run(self):
        """
        Begin door execution. pty.fork() is called, child process
        calls execvpe() while the parent process pipes telnet session
        IPC data to and from the slave pty until child process exits.
        """
        try:
            pid, self.master_fd = pty.fork()
        except OSError, err:
            logger.error ('OSError in pty.fork(): %s', err)
            return

        lines = str(getsession().terminal.height)
        columns = str(getsession().terminal.width)

        # subprocess
        if pid == pty.CHILD:
            sys.stdout.flush ()
            env = { u'LANG': self.lang,
                    u'TERM': self.term,
                    u'PATH': self.path,
                    u'LINES': lines,
                    u'COLUMNS': columns,
                    u'HOME': os.getenv('HOME') }
            try:
                os.execvpe(self.cmd, self.args, env)
            except OSError, err:
                logger.error ('OSError, %s: %s', err, self.args,)
                sys.exit (1)

        # typically, return values from 'input' events are translated keycodes,
        # such as terminal.KEY_ENTER. However, when executing a sub-door, we
        # disable this by setting session.enable_keycodes = False
        chk_keycodes = getsession().enable_keycodes
        getsession().enable_keycodes = False

        # execute self._loop() and catch all i/o and o/s errors
        try:
            logger.info ('exec/%s: %s', pid, ' '.join(self.args))
            self._loop()
        except IOError, err:
            logger.error ('IOError: %s', err)
        except OSError, err:
            # match occurs on read() after child closed sys.stdout. (ok)
            if PATTERN_EIO.search (str(err)) is None:
                # otherwise log as an error,
                logger.error ('OSError: %s', err)

        getsession().enable_keycodes = chk_keycodes

        # retrieve return code
        (pid, status) = os.waitpid (pid, 0)
        res = status >> 8

        if res != 0:
            logger.warn ('child %s has non-zero exit code: %s', pid, res)
        else:
            logger.info ('%s child %s exit %s.', self.cmd, pid, res)

        os.close (self.master_fd)
        return res

    def _loop(self):
        """
        Poll input and outpout of ptys, raising bbs.exception.ConnectionTimeout
        when session idle time exceeds self.timeout.
        """
        term = getsession().terminal
        while True:
            # block up to self.time_opoll for screen output
            rlist = (self.master_fd,)
            ret_tuple = select.select (rlist, (), (), self.time_opoll)
            if self.master_fd in ret_tuple[0]:
                data = os.read(self.master_fd, self.blocksize)
                if 0 == len(data):
                    break
                if self._TAP:
                    logger.debug ('<-- %r', data)
                bbs.output.echo (u''.join((bbs.cp437.CP437[ord(ch)] for ch in
                    data)) if self.decode_cp437 else data.decode('utf8'))

            # block up to self.time_ipoll for keyboard input
            event, data = getsession().read_event (
                    ('refresh','input'), timeout=self.time_ipoll)
            if ((None, None) == (event, data)
                    and getsession().idle > self.timeout):
                raise bbs.exception.ConnectionTimeout ('timeout in door %r',
                        self.args,)
            elif event == 'refresh':
                if data[0] == 'resize':
                    logger.debug ('send TIOCSWINSZ: %dx%d',
                            term.width, term.height)
                    fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ,
                            struct.pack('HHHH', term.height, term.width, 0, 0))
            elif event == 'input':
                if self._TAP:
                    logger.debug ('--> %r' % (data,))
                while 0 != len(data):
                    n_written = os.write(self.master_fd, data)
                    data = data[n_written:]
