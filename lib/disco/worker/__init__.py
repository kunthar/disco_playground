#!/usr/bin/env python
"""
:mod:`disco.worker` -- Python Worker Interface
==============================================

In Disco, :term:`workers <worker>` do the brunt of the data processing work.
When a :class:`disco.job.Job` is created, it gets passed a :class:`Worker` instance,
which is responsible for defining the fields used by the :class:`disco.job.JobPack`.
In most cases, you don't need to define your own Worker subclass in order to run a job.
The Worker classes defined in :mod:`disco` will take care of the details
of creating the fields necessary for the :class:`disco.job.JobPack`,
and when executed on the nodes,
will handle the implementation of the :ref:`worker_protocol`.

There is perhaps a subtle, but important, distinction between
a :term:`worker` and a :class:`Worker`.
The former refers to any binary that gets executed on the nodes,
specified by :attr:`jobdict.worker`.
The latter is a Python class,
which handles details of submitting the job on the client side,
as well as controlling the execution of user-defined code on the nodes.
A :class:`Worker` can be subclassed trivially to create a new :term:`worker`,
without having to worry about fulfilling many of the requirements
for a well-behaving worker.
In short,
a :class:`Worker` provides Python library support for a Disco :term:`worker`.
Those wishing to write a worker in a language besides Python may make use of
the Worker class for submitting jobs to the master,
but generally need to handle the :ref:`worker_protocol`
in the language used for the worker executable.

The :class:`Classic Worker <disco.worker.classic.worker.Worker>`
is a subclass of :class:`Worker`,
which implements the classic Disco :term:`mapreduce` interface.

The following steps illustrate the sequence of events for running a :term:`job`
using a standard :class:`Worker`:

#. (client) instantiate a :class:`disco.job.Job`
        #. if a worker is supplied, use that worker
        #. otherwise, create a worker using :attr:`disco.job.Job.Worker`
           (the default is :class:`disco.worker.classic.worker.Worker`)
#. (client) call :meth:`disco.job.Job.run`
        #. create a :class:`disco.job.JobPack` using:
           :meth:`Worker.jobdict`,
           :meth:`Worker.jobenvs`,
           :meth:`Worker.jobhome`,
           :meth:`Worker.jobdata`
        #. submit the :class:`disco.job.JobPack` to the master
#. (node) master unpacks the :term:`job home`
#. (node) master executes the :attr:`jobdict.worker` with
   current working directory set to the :term:`job home` and
   environment variables set from :ref:`jobenvs`
#. (node) worker requests the :class:`disco.task.Task` from the master
#. (node) worker runs the :term:`task` and reports the output to the master
"""
import cPickle, os, sys, traceback

from disco.error import DataError
from disco.fileutils import DiscoOutput, NonBlockingInput, Wait

class MessageWriter(object):
    def __init__(self, worker):
        self.worker = worker

    @classmethod
    def force_utf8(cls, string):
        if isinstance(string, unicode):
            return string.encode('utf-8', 'replace')
        return string.decode('utf-8', 'replace').encode('utf-8')

    def write(self, string):
        self.worker.send('MSG', self.force_utf8(string.strip()))

class Worker(dict):
    """
    A :class:`Worker` is a :class:`dict` subclass,
    with special methods defined for serializing itself,
    and possibly reinstantiating itself on the nodes where :term:`tasks <task>` are run.

    The :class:`Worker` base class defines the following parameters:

    :type  map: function or None
    :param map: called when the :class:`Worker` is :meth:`run` with a
                :class:`disco.task.Task` in mode *map*.
                Also used by :meth:`jobdict` to set :attr:`jobdict.map?`.

    :type  reduce: function or None
    :param reduce: called when the :class:`Worker` is :meth:`run` with a
                   :class:`disco.task.Task` in mode *reduce*.
                   Also used by :meth:`jobdict` to set :attr:`jobdict.reduce?`.

    :type  required_files: list of paths or dict
    :param required_files: additional files that are required by the worker.
                           Either a list of paths to files to include,
                           or a dictionary which contains items of the form
                           ``(filename, filecontents)``.

                           .. versionchanged:: 0.4
                              The worker includes *required_files* in :meth:`jobzip`,
                              so they are available relative to the working directory
                              of the worker.

    :type  required_modules: list of modules or module names
    :param required_modules: required modules to send with the worker.

                             .. versionchanged:: 0.4
                                Can also be a list of module objects.

    :type  save: bool
    :param save: whether or not to save the output to :ref:`DDFS`.

    :type  profile: bool
    :param profile: determines whether :meth:`run` will be profiled.
    """
    def __init__(self, **kwargs):
        super(Worker, self).__init__(self.defaults())
        self.update(kwargs)
        self.outputs = {}

    @property
    def bin(self):
        """
        The path to the :term:`worker` binary, relative to the :term:`job home`.
        Used to set :attr:`jobdict.worker` in :meth:`jobdict`.
        """
        from inspect import getsourcefile, getmodule
        return getsourcefile(getmodule(self)).strip('/')

    def defaults(self):
        """
        :return: dict of default values for the :class:`Worker`.
        """
        return {'map': None,
                'merge_partitions': False, # XXX: maybe deprecated
                'reduce': None,
                'required_files': {},
                'required_modules': None,
                'save': False,
                'partitions': 1,  # move to classic once partitions are dynamic
                'profile': False}

    def getitem(self, key, job, jobargs, default=None):
        """
        Resolves ``key`` in the following order:
                #. ``jobargs`` (parameters passed in during :meth:`disco.job.Job.run`)
                #. ``job`` (attributes of the :class:`disco.job.Job`)
                #. ``self`` (items in the :class:`Worker` dict itself)
                #. ``default``
        """
        if key in jobargs:
            return jobargs[key]
        elif hasattr(job, key):
            return getattr(job, key)
        return self.get(key, default)

    def jobdict(self, job, **jobargs):
        """
        Creates :ref:`jobdict` for the :class:`Worker`.

        Makes use of the following parameters,
        in addition to those defined by the :class:`Worker` itself:

        :type  input: list of urls or list of list of urls
        :param input: used to set :attr:`jobdict.input`.
                Disco natively handles the following url schemes:

                * ``http://...`` - any HTTP address
                * ``file://...`` or no scheme - a local file.
                    The file must exist on all nodes where the tasks are run.
                    Due to these restrictions, this form has only limited use.
                * ``tag://...`` - a tag stored in :ref:`DDFS`
                * ``raw://...`` - pseudo-address: use the address itself as data.
                * ``dir://...`` - used by Disco internally.
                * ``disco://...`` - used by Disco internally.

                .. seealso:: :mod:`disco.schemes`.

        :type  name: string
        :param name: directly sets :attr:`jobdict.prefix`.

        :type  owner: string
        :param owner: directly sets :attr:`jobdict.owner`.
                      If not specified, uses :envvar:`DISCO_JOB_OWNER`.

        :type  scheduler: dict
        :param scheduler: directly sets :attr:`jobdict.scheduler`.

        Uses :meth:`getitem` to resolve the values of parameters.

        :return: the :term:`job dict`.
        """
        from disco.util import inputlist, ispartitioned, read_index
        def get(key, default=None):
            return self.getitem(key, job, jobargs, default)
        has_map = bool(get('map'))
        has_reduce = bool(get('reduce'))
        input = inputlist(get('input', []),
                          partition=None if has_map else False,
                          settings=job.settings)

        # -- nr_reduces --
        # ignored if there is not actually a reduce specified
        # XXX: master should always handle this
        if has_map:
            # partitioned map has N reduces; non-partitioned map has 1 reduce
            nr_reduces = get('partitions') or 1
        elif ispartitioned(input):
            # no map, with partitions: len(dir://) specifies nr_reduces
            nr_reduces = 1 + max(int(id)
                                 for dir in input
                                 for id, url in read_index(dir))
        else:
            # no map, without partitions can only have 1 reduce
            nr_reduces = 1

        if get('merge_partitions'):
            nr_reduces = 1

        return {'input': input,
                'worker': self.bin,
                'map?': has_map,
                'reduce?': has_reduce,
                'nr_reduces': nr_reduces,
                'prefix': get('name'),
                'scheduler': get('scheduler', {}),
                'owner': get('owner', job.settings['DISCO_JOB_OWNER'])}

    def jobenvs(self, job, **jobargs):
        """
        :return: :ref:`jobenvs` dict.
        """
        settings = job.settings
        settings['LC_ALL'] = 'C'
        settings['PYTHONPATH'] = ':'.join([settings.get('PYTHONPATH', '')] +
                                          [path.strip('/') for path in sys.path])
        return settings.env

    def jobhome(self, job, **jobargs):
        """
        :return: the :term:`job home` (serialized).

        Calls :meth:`jobzip` to create the :class:`disco.fileutils.DiscoZipFile`.
        """
        jobzip = self.jobzip(job, **jobargs)
        jobzip.close()
        return jobzip.dumps()

    def jobzip(self, job, **jobargs):
        """
        A hook provided by the :class:`Worker` for creating the :term:`job home` zip.

        :return: a :class:`disco.fileutils.DiscoZipFile`.
        """
        from clx import __file__ as clxpath
        from disco import __file__ as discopath
        from disco.fileutils import DiscoZipFile
        from disco.util import iskv
        def get(key):
            return self.getitem(key, job, jobargs)
        jobzip = DiscoZipFile()
        jobzip.writepath(os.path.dirname(clxpath))
        jobzip.writepath(os.path.dirname(discopath))
        jobzip.writesource(job)
        jobzip.writesource(self)
        if isinstance(get('required_files'), dict):
            for path, bytes in get('required_files').iteritems():
                    jobzip.writestr(path, bytes)
        else:
            for path in get('required_files'):
                jobzip.writepath(path)
        for mod in get('required_modules') or ():
            jobzip.writemodule((mod[0] if iskv(mod) else mod))
        return jobzip

    def jobdata(self, job, **jobargs):
        """
        :return: :ref:`jobdata` needed for instantiating the :class:`Worker` on the node.
        """
        return cPickle.dumps((self, job, jobargs), -1)

    def input(self, task, merged=False, **kwds):
        """
        :type  merged: bool
        :param merged: if specified, returns a :class:`MergedInput`.

        :type  kwds: dict
        :param kwds: additional keyword arguments for the :class:`Input`.

        :return: a :class:`Input` to iterate over the inputs from the master.
        """
        if merged:
            return MergedInput(self.get_inputs(), **kwds)
        return SerialInput(self.get_inputs(), **kwds)

    def output(self, task, partition=None, **kwds):
        """
        :type  partition: string or None
        :param partition: the label of the output partition to get.

        :type  kwds: dict
        :param kwds: additional keyword arguments for the :class:`Output`.

        :return: the previously opened :class:`Output` for *partition*,
                 or if necessary, a newly opened one.
        """
        if partition not in self.outputs:
            self.outputs[partition] = Output(task.output(partition=partition), **kwds)
        return self.outputs[partition]

    def start(self, task, job, **jobargs):
        from disco.sysutil import set_mem_limit
        set_mem_limit(job.settings['DISCO_WORKER_MAX_MEM'])
        task.makedirs()
        if self.getitem('profile', job, jobargs):
            from cProfile import runctx
            name = 'profile-%s' % task.uid
            path = task.path(name)
            runctx('self.run(task, job, **jobargs)', globals(), locals(), path)
            task.put(name, open(path).read())
        else:
            self.run(task, job, **jobargs)
        self.end(task, job, **jobargs)

    def run(self, task, job, **jobargs):
        """
        Called to do the actual work of processing the :class:`disco.task.Task`.
        """
        self.getitem(task.mode, job, jobargs)(task, job, **jobargs)

    def end(self, task, job, **jobargs):
        if not self['save'] or (task.mode == 'map' and self['reduce']):
            self.send_outputs()
            self.send('MSG', "Results sent to master")
        else:
            self.save_outputs(task.jobname, master=task.master)
            self.send('MSG', "Results saved to DDFS")

    @classmethod
    def main(cls):
        """
        The main method used to bootstrap the :class:`Worker` when it is being executed.

        It is enough for the module to define::

                if __name__ == '__main__':
                    Worker.main()

        .. note:: It is critical that subclasses check if they are executing
                  in the ``__main__`` module, before running :meth:`main`,
                  as the worker module is also generally imported on the client side.
        """
        try:
            sys.stdin = NonBlockingInput(sys.stdin, timeout=600)
            sys.stdout = MessageWriter(cls)
            cls.send('PID', os.getpid())
            worker, job, jobargs = cls.unpack(cls.get_jobpack())
            worker.start(cls.get_task(), job, **jobargs)
            cls.send('END')
        except (DataError, EnvironmentError, MemoryError), e:
            # check the number of open file descriptors (under proc), warn if close to max
            # http://stackoverflow.com/questions/899038/getting-the-highest-allocated-file-descriptor
            # also check for other known reasons for error, such as if disk is full
            cls.send('DAT', traceback.format_exc())
            raise
        except Exception, e:
            cls.send('ERR', MessageWriter.force_utf8(traceback.format_exc()))
            raise

    @classmethod
    def unpack(cls, jobpack):
        return cPickle.loads(jobpack.jobdata)

    @classmethod
    def send(cls, type, payload=''):
        from disco.json import dumps, loads
        body = dumps(payload)
        sys.stderr.write('%s %d %s\n' % (type, len(body), body))
        spent, rtype = sys.stdin.t_read_until(' ')
        spent, rsize = sys.stdin.t_read_until(' ', spent=spent)
        spent, rbody = sys.stdin.t_read(int(rsize) + 1, spent=spent)
        if type == 'ERROR':
            raise ValueError(loads(rbody[:-1]))
        return loads(rbody[:-1])

    @classmethod
    def get_input(cls, id):
        done, inputs = cls.send('INP', ['include', [id]])
        _id, status, replicas = inputs[0]

        if status == 'busy':
            raise Wait
        if status == 'failed':
            raise DataError("Can't handle broken input", id)
        return [url for rid, url in replicas]

    @classmethod
    def get_inputs(cls, done=False, exclude=()):
        while not done:
            done, inputs = cls.send('INP')
            for id, status, urls in inputs:
                if id not in exclude:
                    yield IDedInput((cls, id))
                    exclude += (id, )

    @classmethod
    def get_jobpack(cls):
        from disco.job import JobPack
        return JobPack.load(open(cls.send('JOB')))

    @classmethod
    def get_task(cls):
        from disco.task import Task
        return Task(**dict((str(k), v) for k, v in cls.send('TSK').items()))

    def save_outputs(self, jobname, master=None):
        from disco.ddfs import DDFS
        def paths():
            for output in self.outputs.values():
                output.file.close()
                yield output.path
        self.send('OUT', [DDFS(master).save(jobname, paths()), 'tag'])

    def send_outputs(self):
        for output in self.outputs.values():
            output.file.close()
            self.send('OUT', [output.path, output.type, output.partition])

class IDedInput(tuple):
    @property
    def worker(self):
        return self[0]

    @property
    def id(self):
        return self[1]

    @property
    def urls(self):
        return self.worker.get_input(self.id)

    def unavailable(self, tried):
        return self.worker.send('DAT', [self.id, list(tried)])

class ReplicaIter(object):
    def __init__(self, inp_or_urls):
        self.inp, self.urls = None, None
        if isinstance(inp_or_urls, IDedInput):
            self.inp = inp_or_urls
        elif isinstance(inp_or_urls, basestring):
            self.urls = inp_or_urls,
        else:
            self.urls = inp_or_urls
        self.used = set()

    def __iter__(self):
        return self

    def next(self):
        urls = set(self.inp.urls if self.inp else self.urls) - self.used
        for url in urls:
            self.used.add(url)
            return url
        if self.inp:
            self.inp.unavailable(self.used)
        raise StopIteration

class InputIter(object):
    def __init__(self, input, open=None, start=0):
        self.urls = ReplicaIter(input)
        self.last = start - 1
        self.open = open if open else Input.default_open
        self.swap()

    def __iter__(self):
        return self

    def next(self):
        try:
            self.last, item = self.iter.next()
            return item
        except DataError:
            self.swap()

    def swap(self):
        try:
            def skip(iter, N):
                from itertools import dropwhile
                return dropwhile(lambda (n, rec): n < N, enumerate(iter))
            self.iter = skip(self.open(self.urls.next()), self.last + 1)
        except DataError:
            self.swap()
        except StopIteration:
            raise DataError("Exhausted all available replicas", list(self.urls.used))

class Input(object):
    """
    An iterable over one or more :class:`Task` inputs,
    which can gracefully handle corrupted replicas or otherwise failed inputs.

    :type  open: function
    :param open: a function with the following signature::

                        def open(url):
                            ...
                            return file

                used to open input files.
    """
    WAIT_TIMEOUT = 1

    def __init__(self, input, **kwds):
        self.input, self.kwds = input, kwds

    def __iter__(self):
        iter = InputIter(self.input, **self.kwds)
        while iter:
            try:
                for item in iter:
                    yield item
                iter = None
            except Wait:
                time.sleep(self.WAIT_TIMEOUT)

    @staticmethod
    def default_open(url):
        from disco.util import schemesplit
        scheme, _url = schemesplit(url)
        scheme_ = 'scheme_%s' % (scheme or 'file')
        mod = __import__('disco.schemes.%s' % scheme_, fromlist=[scheme_])
        file, size, url = mod.input_stream(None, None, url, None)
        return file

class Output(object):
    """
    A container for outputs from :class:`tasks <Task>`.

    :type  open: function
    :param open: a function with the following signature::

                        def open(url):
                            ...
                            return file

                used to open new output files.

    .. attribute:: path

        The path to the underlying output file.

    .. attribute:: type

        The type of output.

    .. attribute:: partition

        The partition label for the output (or None).

    .. attribute:: file

        The underlying output file handle.
    """
    def __init__(self, (path, type, partition), open=None):
        self.path, self.type, self.partition = path, type, partition
        self.open = open or DiscoOutput
        self.file = self.open(self.path)

class SerialInput(Input):
    """
    Produces an iterator over the records in a list of sequential inputs.
    """
    def __init__(self, inputs, **kwds):
        self.inputs, self.kwds = inputs, kwds

    def __iter__(self):
        for input in self.inputs:
            for record in Input(input, **self.kwds):
                yield record

class ParallelInput(Input):
    """
    Produces an iterator over the unordered records in a set of inputs.

    Usually require the full set of inputs (i.e. will block with streaming).
    """
    def __init__(self, inputs, **kwds):
        self.inputs, self.kwds = inputs, kwds

    def __iter__(self):
        iters = [InputIter(input, **self.kwds) for input in self.inputs]
        while iters:
            iter = iters.pop()
            try:
                for item in iter:
                    yield item
            except Wait:
                if not iters:
                    time.sleep(self.WAIT_TIMEOUT)
                iters.insert(0, iter)

    def couple(self, iters, heads, n):
        while True:
            if heads[n] is Wait:
                self.fill(iters, heads, n=n)
            head = heads[n]
            heads[n] = Wait
            yield head

    def fetch(self, iters, heads, stop=all):
        busy = 0
        for n, head in enumerate(heads):
            if head is Wait:
                try:
                    heads[n] = next(iters[n])
                except Wait:
                    if stop in (all, n):
                        busy += 1
                except StopIteration:
                    if stop in (all, n):
                        raise
        return busy

    def fill(self, iters, heads, n=all, busy=True):
        while busy:
            busy = self.fetch(iters, heads, stop=n)
            if busy:
                time.sleep(self.WAIT_TIMEOUT)
        return heads

class MergedInput(ParallelInput):
    """
    Produces an iterator over the minimal head elements of the inputs.
    """
    def __iter__(self):
        from disco.future import merge
        iters = [InputIter(input, **self.kwds) for input in self.inputs]
        heads = [Wait] * len(iters)
        return merge(*(self.couple(iters, heads, n) for n in xrange(len(iters))))

if __name__ == '__main__':
    Worker.main()
