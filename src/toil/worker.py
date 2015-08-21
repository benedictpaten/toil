#!/usr/bin/env python

#Copyright (C) 2011 by Benedict Paten (benedictpaten@gmail.com)
#
#Permission is hereby granted, free of charge, to any person obtaining a copy
#of this software and associated documentation files (the "Software"), to deal
#in the Software without restriction, including without limitation the rights
#to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#copies of the Software, and to permit persons to whom the Software is
#furnished to do so, subject to the following conditions:
#
#The above copyright notice and this permission notice shall be included in
#all copies or substantial portions of the Software.
#
#THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
#THE SOFTWARE.

from __future__ import absolute_import
import os
import sys

if __name__ == "__main__":
    # FIXME: Until we use setuptools entry points, this is the only way to avoid a conflict between our own resource.py
    # and Python's
    sys.path.remove(os.path.dirname(os.path.abspath(__file__)))

import traceback
import importlib
import time
import socket
import logging
import xml.etree.cElementTree as ET
import cPickle
import shutil

logger = logging.getLogger( __name__ )

def truncateFile(fileNameString, tooBig=50000):
    """
    Truncates a file that is bigger than tooBig bytes, leaving only the 
    last tooBig bytes in the file.
    """
    if os.path.getsize(fileNameString) > tooBig:
        fh = open(fileNameString, 'rb+')
        fh.seek(-tooBig, 2) 
        data = fh.read()
        fh.seek(0) # rewind
        fh.write(data)
        fh.truncate()
        fh.close()

def loadJob(command, jobStore):
    """
    Unpickles a job.Job instance by decoding the command.
    See job.Job._serialiseFirstJob and job.Job._serialiseFirstJob
    job.Job._makeJobWrappers to see how the Job is encoded in the command.
    Essentially the command is a reference to a jobStoreFileID containing 
    the pickle file for the job and a list of modules which must be imported
    so that the Job can be successfully unpickled.
    """
    commandTokens = command.split()
    assert "scriptTree" == commandTokens[0]
    pickleFile = commandTokens[1]
    jobClassName = commandTokens[2]
    # must import lazily because toil might not be on sys.path when the top-level of this module is run
    from toil.resource import ModuleDescriptor
    userModule = ModuleDescriptor(*commandTokens[3:])
    if not userModule.belongsToToil:
        userModule = userModule.localize()
    if userModule.dirPath not in sys.path:
        sys.path.append(userModule.dirPath)
    userModule = importlib.import_module(userModule.name)
    thisModule = sys.modules[__name__]
    thisModule.__dict__[jobClassName] = userModule.__dict__[jobClassName]
    if pickleFile == "firstJob":
        openFileStream = jobStore.readSharedFileStream( pickleFile )
    else:
        openFileStream = jobStore.readFileStream( pickleFile )
    with openFileStream as fileHandle:
        return cPickle.load( fileHandle )
    
def nextOpenDescriptor():
    """Gets the number of the next available file descriptor.
    """
    descriptor = os.open("/dev/null", os.O_RDONLY)
    os.close(descriptor)
    return descriptor
    
def main():
    ########################################## 
    #Import necessary modules 
    ##########################################
    
    # This is assuming that worker.py is at a path ending in "/toil/worker.py".
    sourcePath = os.path.dirname(os.path.dirname(__file__))
    if sourcePath not in sys.path:
        # FIXME: prepending to sys.path should fix #103
        sys.path.append(sourcePath)
    
    #Now we can import all the necessary functions
    from toil.lib.bioio import setLogLevel
    from toil.lib.bioio import getTotalCpuTime
    from toil.lib.bioio import getTotalCpuTimeAndMemoryUsage
    from toil.lib.bioio import getTempDirectory
    from toil.lib.bioio import makePublicDir
    from toil.lib.bioio import system
    from toil.common import loadJobStore
    
    ########################################## 
    #Input args
    ##########################################
    
    jobStoreString = sys.argv[1]
    jobStoreID = sys.argv[2]
    
    ##########################################
    #Load the jobStore/config file
    ##########################################
    
    jobStore = loadJobStore(jobStoreString)
    config = jobStore.config

    ##########################################
    #Load the environment for the job
    ##########################################
    
    #First load the environment for the job.
    with jobStore.readSharedFileStream("environment.pickle") as fileHandle:
        environment = cPickle.load(fileHandle)
    for i in environment:
        if i not in ("TMPDIR", "TMP", "HOSTNAME", "HOSTTYPE"):
            os.environ[i] = environment[i]
    # sys.path is used by __import__ to find modules
    if "PYTHONPATH" in environment:
        for e in environment["PYTHONPATH"].split(':'):
            if e != '':
                sys.path.append(e)

    setLogLevel(config.logLevel)

    tempRootDir = config.workDir
    if tempRootDir is not None and not os.path.exists(tempRootDir):
        raise RuntimeError("The temporary directory specified by workDir: %s does not exist" % tempRootDir)

    ##########################################
    #Setup the temporary directories.
    ##########################################
        
    #Dir to put all the temp files in. If tempRootDir is None, tempdir looks at environment variables to determine
    # where to put the tempDir.
    localWorkerTempDir = getTempDirectory(tempRootDir)
    
    ##########################################
    #Setup the logging
    ##########################################

    #This is mildly tricky because we don't just want to
    #redirect stdout and stderr for this Python process; we want to redirect it
    #for this process and all children. Consequently, we can't just replace
    #sys.stdout and sys.stderr; we need to mess with the underlying OS-level
    #file descriptors. See <http://stackoverflow.com/a/11632982/402891>
    
    #When we start, standard input is file descriptor 0, standard output is
    #file descriptor 1, and standard error is file descriptor 2.

    #What file do we want to point FDs 1 and 2 to?    
    tempWorkerLogPath = os.path.join(localWorkerTempDir, "worker_log.txt")
    
    #Save the original stdout and stderr (by opening new file descriptors to the
    #same files)
    origStdOut = os.dup(1)
    origStdErr = os.dup(2)
    
    #Open the file to send stdout/stderr to.
    logFh = os.open(tempWorkerLogPath, os.O_WRONLY | os.O_CREAT | os.O_APPEND)

    #Replace standard output with a descriptor for the log file
    os.dup2(logFh, 1)
    
    #Replace standard error with a descriptor for the log file
    os.dup2(logFh, 2)
    
    #Since we only opened the file once, all the descriptors duped from the
    #original will share offset information, and won't clobber each others'
    #writes. See <http://stackoverflow.com/a/5284108/402891>. This shouldn't
    #matter, since O_APPEND seeks to the end of the file before every write, but
    #maybe there's something odd going on...
    
    #Close the descriptor we used to open the file
    os.close(logFh)
    
    for handler in list(logger.handlers): #Remove old handlers
        logger.removeHandler(handler)
    
    #Add the new handler. The sys.stderr stream has been redirected by swapping
    #the file descriptor out from under it.
    logger.addHandler(logging.StreamHandler(sys.stderr))

    ##########################################
    #Worker log file trapped from here on in
    ##########################################

    workerFailed = False
    try:

        #Put a message at the top of the log, just to make sure it's working.
        print "---TOIL WORKER OUTPUT LOG---"
        sys.stdout.flush()
        
        #Log the number of open file descriptors so we can tell if we're leaking
        #them.
        logger.debug("Next available file descriptor: {}".format(
            nextOpenDescriptor()))
    
        ##########################################
        #Load the job
        ##########################################
        
        job = jobStore.load(jobStoreID)
        logger.debug("Parsed job")
        
        ##########################################
        #Cleanup from any earlier invocation of the job
        ##########################################
        
        if job.command == None:
            while len(job.stack) > 0:
                jobs = job.stack[-1]
                #If the jobs still exist they have not been run, so break
                if jobStore.exists(jobs[0][0]):
                    break
                #However, if they are gone then we can remove them from the stack.
                #This is the only way to flush successors that have previously been run
                #, as jobs are, as far as possible, read only in the leader.
                job.stack.pop()
                
                
        #This cleans the old log file which may 
        #have been left if the job is being retried after a job failure.
        if job.logJobStoreFileID != None:
            job.clearLogFile(jobStore)
    
        ##########################################
        #Setup the stats, if requested
        ##########################################
        
        if config.stats:
            startTime = time.time()
            startClock = getTotalCpuTime()
            stats = ET.Element("worker")
        else:
            stats = None

        startTime = time.time() 
        while True:
            ##########################################
            #Run the job, if there is one
            ##########################################
            
            if job.command != None:
                if job.command[:11] == "scriptTree ":
                    #Make a temporary file directory for the job
                    localTempDir = makePublicDir(os.path.join(localWorkerTempDir, "localTempDir"))
                    
                    #Is a job command
                    messages = loadJob(job.command, jobStore)._execute( jobWrapper=job,
                                    stats=stats, localTempDir=localTempDir, 
                                    jobStore=jobStore)
                    
                    #Remove the temporary file directory
                    shutil.rmtree(localTempDir)
    
                else: #Is another command (running outside of jobs may be deprecated)
                    system(job.command)
                    messages = []
            else:
                #The command may be none, in which case
                #the job is just a shell ready to be deleted
                assert len(job.stack) == 0
                messages = []
                break
            
            ##########################################
            #Establish if we can run another job within the worker
            ##########################################
            
            #No more jobs to run so quit
            if len(job.stack) == 0:
                break
            
            #Get the next set of jobs to run
            jobs = job.stack[-1]
            assert len(jobs) > 0
            
            #If there are 2 or more jobs to run in parallel we quit
            if len(jobs) >= 2:
                logger.debug("No more jobs can run in series by this worker,"
                            " it's got %i children", len(jobs)-1)
                break
            
            #We check the requirements of the job to see if we can run it
            #within the current worker
            successorJobStoreID, successorMemory, successorCpu, successorsDisk, successorPredecessorID = jobs[0]
            if successorMemory > job.memory:
                logger.debug("We need more memory for the next job, so finishing")
                break
            if successorCpu > job.cpu:
                logger.debug("We need more cpus for the next job, so finishing")
                break
            if successorsDisk > job.disk:
                logger.debug("We need more disk for the next job, so finishing")
                break
            if successorPredecessorID != None: 
                logger.debug("The job has multiple predecessors, we must return to the leader.")
                break
          
            ##########################################
            #We have a single successor job.
            #We load the successor job and transplant its command and stack
            #into the current job so that it can be run
            #as if it were a command that were part of the current job.
            #We can then delete the successor job in the jobStore, as it is
            #wholly incorporated into the current job.
            ##########################################
            
            #Remove the successor job
            job.stack.pop()
            
            #Load the successor job
            successorJob = jobStore.load(successorJobStoreID)
            #These should all match up
            assert successorJob.memory == successorMemory
            assert successorJob.cpu == successorCpu
            assert successorJob.predecessorsFinished == set()
            assert successorJob.predecessorNumber == 1
            assert successorJob.command != None
            assert successorJobStoreID == successorJob.jobStoreID
            
            #Transplant the command and stack to the current job
            job.command = successorJob.command
            job.stack += successorJob.stack
            assert job.memory >= successorJob.memory
            assert job.cpu >= successorJob.cpu
            
            #Checkpoint the job and delete the successorJob
            job.jobsToDelete = [ successorJob.jobStoreID ]
            jobStore.update(job)
            jobStore.delete(successorJob.jobStoreID)
            
            logger.debug("Starting the next job")
        
        ##########################################
        #Finish up the stats
        ##########################################

        if stats != None:
            totalCpuTime, totalMemoryUsage = getTotalCpuTimeAndMemoryUsage()
            stats.attrib["time"] = str(time.time() - startTime)
            stats.attrib["clock"] = str(totalCpuTime - startClock)
            stats.attrib["memory"] = str(totalMemoryUsage)
            m = ET.SubElement(stats, "messages")
            for message in messages:
                ET.SubElement(m, "message").text = message
            jobStore.writeStatsAndLogging(ET.tostring(stats))
        elif len(messages) > 0: #No stats, but still need to report log messages
            l = ET.Element("worker")
            m = ET.SubElement(l, "messages")
            for message in messages:
                ET.SubElement(m, "message").text = message
            jobStore.writeStatsAndLogging(ET.tostring(l))
        
        logger.info("Finished running the chain of jobs on this node, we ran for a total of %f seconds", time.time() - startTime)
    
    ##########################################
    #Trapping where worker goes wrong
    ##########################################
    except: #Case that something goes wrong in worker
        traceback.print_exc()
        logger.error("Exiting the worker because of a failed job on host %s", socket.gethostname())
        job = jobStore.load(jobStoreID)
        job.setupJobAfterFailure(config)
        workerFailed = True

    ##########################################
    #Cleanup
    ##########################################
    
    #Close the worker logging
    #Flush at the Python level
    sys.stdout.flush()
    sys.stderr.flush()
    #Flush at the OS level
    os.fsync(1)
    os.fsync(2)
    
    #Close redirected stdout and replace with the original standard output.
    os.dup2(origStdOut, 1)
    
    #Close redirected stderr and replace with the original standard error.
    os.dup2(origStdOut, 2)
    
    #sys.stdout and sys.stderr don't need to be modified at all. We don't need
    #to call redirectLoggerStreamHandlers since they still log to sys.stderr
    
    #Close our extra handles to the original standard output and standard error
    #streams, so we don't leak file handles.
    os.close(origStdOut)
    os.close(origStdErr)
    
    #Now our file handles are in exactly the state they were in before.
    
    #Copy back the log file to the global dir, if needed
    if workerFailed:
        truncateFile(tempWorkerLogPath)
        job.setLogFile(tempWorkerLogPath, jobStore)
        os.remove(tempWorkerLogPath)
        jobStore.update(job)

    #Remove the temp dir
    shutil.rmtree(localWorkerTempDir)
    
    #This must happen after the log file is done with, else there is no place to put the log
    if (not workerFailed) and job.command == None and len(job.stack) == 0:
        #We can now safely get rid of the job
        jobStore.delete(job.jobStoreID)
       
if __name__ == '__main__':
    logging.basicConfig()
    main()
