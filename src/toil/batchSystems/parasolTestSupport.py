import logging
import os
import subprocess
from toil.batchSystems.parasol import popenParasolCommand
from toil.lib.bioio import getTempFile


class ParasolTestSupport(object):
    def _startParasol(self):
        self._stopParasol()
        self.machineList = getTempFile(rootDir=os.getcwd())
        with open(self.machineList, "w") as output:
            output.write("localhost 2 1024 /tmp /scratch 36000 r1")
        self.startNode()
        self.startHub()
    def _stopParasol(self):
        self.stopNode()
        self.stopHub()
    def startNode(self):
        return popenParasolCommand("paraNode start")
    def stopNode(self):
        return popenParasolCommand("paraNode stop")
    def startHub(self):
        return popenParasolCommand("paraHub %s &" % self.machineList)
    def stopHub(self):
        return popenParasolCommand("paraHubStop now")
        os.remove(self.machineList)
