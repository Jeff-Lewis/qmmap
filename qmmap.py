#
# mongo Operations
#
import sys, os, importlib, datetime, time, traceback, __main__
import pymongo
import threading
import mongoengine as meng
from mongoengine.context_managers import switch_collection

NULL = open(os.devnull, "w")

def is_shell():
    return sys.argv[0] == "" or sys.argv[0][-8:] == "/ipython"

class housekeep(meng.Document):
    start = meng.DynamicField(primary_key = True)
    end = meng.DynamicField()
    total = meng.IntField()                             # total # of entries to do
    good = meng.IntField(default = 0)                   # entries successfully processed
#     bad = meng.IntField(default = 0)                    # entries we failed to process to completion
#     log = meng.ListField()                              # log of misery -- each item a failed processing incident
    state = meng.StringField(default = 'open')
#     git = meng.StringField()                                 # git commit of this version of source_destination
    tstart = meng.DateTimeField()  # Time when job started
    time = meng.DateTimeField()  # Time when job finished
    meta = {'indexes': ['state', 'time']}

def _connect(srccol, destcol):
    connectMongoEngine(destcol)
    hk_colname = srccol.name + '_' + destcol.name
    switch_collection(housekeep, hk_colname).__enter__()

def _init(srccol, destcol, key, query, chunk_size, verbose):
    housekeep.drop_collection()
    q = srccol.find(query, [key]).sort([(key, pymongo.ASCENDING)])
    if verbose & 2: print "initializing %d entries, housekeeping for %s" % (q.count(), housekeep._get_collection_name())
#     else:
#         raise Exception("no incremental yet")
# #         last = housekeep.objects().order_by('-start')[0].end
# #         if verbose & 2: print "last partition field in housekeep:", last
# #         query[key + "__gt"] = last
# #         q = srccol.objects(**query).only(key).order_by(key)
# #         if verbose & 2: print "added %d entries to %s" % (q.count(), housekeep._get_collection_name())
# #         sys.stdout.flush()
    i = 0
    tot = q.limit(chunk_size).count()
    while tot > 0:
        if verbose & 2: print "housekeeping: %d" % i
        i +=1
        sys.stdout.flush()
        hk = housekeep()
        hk.start = q[0][key]
        hk.end =  q[min(chunk_size-1, tot-1)][key]
        if (hk.start == None or hk.end == None):
            if verbose & 2: print >> sys.stderr, "ERROR: key field has None. start: %s end: %s" % (hk.start, hk.end)
            raise Exception("key error")
        #calc total for this segment
        qq = {'$and': [query, {key: {'$gte': hk.start}}, {key: {'$lte': hk.end}}]}
        hk.total = srccol.find(qq, [key]).count()
        hk.save()

        #get start of next segment
        qq = {'$and': [query, {key: {'$gt': hk.end}}]}
        q = srccol.find(qq, [key]).sort([(key, pymongo.ASCENDING)])
        tot = q.limit(chunk_size).count()                    #limit count to chunk for speed

def _process(init, proc, src, dest, verbose):
    if not verbose & 1:
        oldstdout = sys.stdout
        sys.stdout = NULL
    if init:
        try:
            init(src, dest)
        except:
            print >> sys.stderr, "***EXCEPTION (process)***"
            print >> sys.stderr, traceback.format_exc()
            print >> sys.stderr, "***END EXCEPTION***"
            return 0
    good = 0
    for doc in src:
        try:
            ret = proc(doc)
            if ret != None:
                dest.save(ret)
            good += 1
        except:
            print >> sys.stderr, "***EXCEPTION (process)***"
            print >> sys.stderr, traceback.format_exc()
            print >> sys.stderr, "***END EXCEPTION***"
    if not verbose & 1:
        sys.stdout = oldstdout
    return good

def do_chunks(init, proc, src_col, dest_col, query, key, verbose):
    while housekeep.objects(state = 'done').count() < housekeep.objects.count():
        tnow = datetime.datetime.utcnow()
        raw = housekeep._collection.find_and_modify(
            {'state': 'open'},
            {
                '$set': {
                    'state': 'working',
                    'tstart': tnow,
                }
            }
        )
        #if raw==None, someone scooped us
        if raw != None:
            #reload as mongoengine object -- _id is .start (because set as primary_key)
            hko = housekeep.objects(start = raw['_id'])[0]
            # Record git commit for sanity
#             hko.git = git.Git('.').rev_parse('HEAD')
#             hko.save()
            # get data pointed to by housekeep
            qq = {'$and': [query, {key: {'$gte': hko.start}}, {key: {'$lte': hko.end}}]}
            cursor = src_col.find(qq)
            if verbose & 2: print "mongo_process: %d elements in chunk %s-%s" % (cursor.count(), hko.start, hko.end)
            sys.stdout.flush()
            # This is where processing happens
            hko.good =_process(init, proc, cursor, dest_col, verbose)
            hko.state = 'done'
            hko.time = datetime.datetime.utcnow()
            hko.save()
#         else:
#             if verbose & 2: print "race lost -- skipping"
#         if verbose & 2: print "sleep..."
        sys.stdout.flush()
        time.sleep(0.1)
#
# balance chunk size vs async efficiency etc
# min 10 obj per chunk
# max 600 obj per chunk
# otherwise try for at least 10 chunks per proc
#
def _calc_chunksize(count, multi):
    cs = count/(multi*10.0)
#     if verbose & 2: print "\ninitial size:", cs
    cs = max(cs, 10)
    cs = min(cs, 600)
    if count / float(cs * multi) < 1.0:
        cs *= count / float(cs * multi)
        cs = max(1, int(cs))
#     if verbose & 2: print "obj count:", count
#     if verbose & 2: print "multi proc:", multi
#     if verbose & 2: print "chunk size:", cs
#     if verbose & 2: print "chunk count:", count / float(cs)
#     if verbose & 2: print "per proc:", count / float(cs * multi)
    return int(cs)

# cs = _calc_chunksize(11, 3)
# cs = _calc_chunksize(20, 1)
# cs = _calc_chunksize(1000, 5)
# cs = _calc_chunksize(1000, 15)
# cs = _calc_chunksize(1000, 150)
# cs = _calc_chunksize(100000, 5)
# cs = _calc_chunksize(100000, 15)
# cs = _calc_chunksize(100000, 150)
# exit()

def mmap(   cb,
            source_col,
            dest_col,
            init=None, 
            source_uri="mongodb://127.0.0.1/test", 
            dest_uri="mongodb://127.0.0.1/test",
            query={},
            key='_id',
            verbose=1,
            multi=None,
            wait_done=True,
            init_only=False,
            process_only=False,
            timeout=120):

    dbs = pymongo.MongoClient(source_uri).get_default_database()
    dbd = pymongo.MongoClient(dest_uri).get_default_database()
    dest = dbd[dest_col]
    if multi == None:           #don't use housekeeping, run straight process

        source = dbs[source_col].find(query)
        _process(init, cb, source, dest, verbose)
    else:
        _connect(dbs[source_col], dest)
        if not process_only:
            chunk_size = _calc_chunksize(dbs[source_col].count(), multi)
            if verbose & 2: print "chunk size:", chunk_size
            _init(dbs[source_col], dest, key, query, chunk_size, verbose)
        if not init_only:
            args = (init, cb, dbs[source_col], dest, query, key, verbose)
            if verbose & 2:
                print "Chunking with arguments %s" % args
            if is_shell():
                print >> sys.stderr, ("WARNING -- can't generate module name. Multiprocessing will be emulated...")
                do_chunks(*args)
            else:
                for j in xrange(multi):
                    if verbose & 2:
                        print "Launching subprocess %s" % j
                    threading.Thread(target=do_chunks, args=args).start()
            if wait_done:
                wait(timeout, verbose & 2)
    return dbd[dest_col]

def toMongoEngine(pmobj, metype):
    meobj = metype._from_son(pmobj)
    meobj.validate()
    return meobj

def connectMongoEngine(pmcol):
    if pymongo.version_tuple[0] == 2:     #really? REALLY?
        #host = pmcol.database.connection.HOST
        #port = pmcol.database.connection.PORT
        host = pmcol.database.connection.host
        port = pmcol.database.connection.port
    else:
        host = pmcol.database.client.HOST
        port = pmcol.database.client.PORT
    return meng.connect(pmcol.database.name, host=host, port=port)

def remaining():
    return housekeep.objects(state__ne = "done").count()

def wait(timeout=120, verbose=True):
    t = time.time()
    r = remaining()
    rr = r
    while r:
#         print "DEBUG r %f rr %f t %f" % (r, rr, time.time() - t)
        if time.time() - t > timeout:
            if verbose: print >> sys.stderr, "TIMEOUT reached - resetting working chunks to open"
            q = housekeep.objects(state = "working")
            if q:
                q.update(state = "open")
        if r != rr:
            t = time.time()
        if verbose: print r, "chunks remaning to be processed; %f seconds left until timeout" % (timeout - (time.time() - t)) 
        time.sleep(1)
        rr = r
        r = remaining()
