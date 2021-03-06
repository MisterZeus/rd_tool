#!/usr/bin/env python

from __future__ import print_function

import argparse
import os
import sys
import threading
import subprocess
from time import sleep
from datetime import datetime
import multiprocessing
import boto.ec2.autoscale
from pprint import pprint
import json

#our timestamping function, accurate to milliseconds
#(remove [:-3] to display microseconds)
def GetTime():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

if 'DAALA_ROOT' not in os.environ:
    print(GetTime(),"Please specify the DAALA_ROOT environment variable to use this tool.")
    sys.exit(1)

daala_root = os.environ['DAALA_ROOT']

#the AWS instances
class Machine:
    def __init__(self,host):
        self.host = host
    def setup(self):
        print(GetTime(),'Connecting to',self.host)
        if subprocess.call(['./transfer_git.sh',self.host]) != 0:
          print(GetTime(),'Couldn\'t set up machine '+self.host)
          sys.exit(1)
    def execute(self,command):
        ssh_command = ['ssh','-i','daala.pem','-o',' StrictHostKeyChecking=no',command]
    def upload(self,filename):
        basename = os.path.basename(filename)
        print(GetTime(),'Uploading',basename)
        subprocess.call(['scp','-i','daala.pem','-o',' StrictHostKeyChecking=no',filename,
            'ec2-user@'+self.host+':/home/ec2-user/video/'+basename])

def shellquote(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"

#the job slots we can fill
class Slot:
    def __init__(self, machine=None):
        self.name='localhost'
        self.machine = machine
        self.p = None
    def execute(self, work):
        self.work = work
        output_name = work.filename+'.'+str(work.quality)+'.ogv'
        input_path = '/home/ec2-user/sets/'+self.work.set+'/'+self.work.filename
        env = {}
        env['DAALA_ROOT'] = daala_root
        env['x'] = str(work.quality)
        print(GetTime(),'Encoding',work.filename,'with quality',work.quality,'on',self.machine.host)
        if self.machine is None:
            print(GetTime(),'No support for local execution.')
            sys.exit(1)
            self.p = subprocess.Popen(['metrics_gather.sh',work.filename], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        else:
            self.p = subprocess.Popen(['ssh','-i','daala.pem','-o',' StrictHostKeyChecking=no',
                'ec2-user@'+self.machine.host,
                ('DAALA_ROOT=/home/ec2-user/daala/ x="'+str(work.quality)+'" CODEC="'+args.codec+
                    '" /home/ec2-user/rd_tool/metrics_gather.sh '+shellquote(input_path)
                ).encode("utf-8")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    def busy(self):
        if self.p is None:
            return False
        elif self.p.poll() is None:
            return True
        else:
            return False
    def gather(self):
        (stdout, stderr) = self.p.communicate()
        self.work.raw = stdout
        self.work.parse()

class Work:
    def parse(self):
        split = None
        try:
            split = self.raw.decode('utf-8').replace(')',' ').split()
            self.pixels = split[1]
            self.size = split[2]
            self.metric = {}
            self.metric['psnr'] = {}
            self.metric["psnr"][0] = split[6]
            self.metric["psnr"][1] = split[8]
            self.metric["psnr"][2] = split[10]
            self.metric['psnrhvs'] = {}
            self.metric["psnrhvs"][0] = split[14]
            self.metric["psnrhvs"][1] = split[16]
            self.metric["psnrhvs"][2] = split[18]
            self.metric['ssim'] = {}
            self.metric["ssim"][0] = split[22]
            self.metric["ssim"][1] = split[24]
            self.metric["ssim"][2] = split[26]
            self.metric['fastssim'] = {}
            self.metric["fastssim"][0] = split[30]
            self.metric["fastssim"][1] = split[32]
            self.metric["fastssim"][2] = split[34]
            self.failed = False
        except IndexError:
            print(GetTime(),'Decoding result data failed! Result was:')
            print(GetTime(),self.raw.decode('utf-8'))
            self.failed = True

#set up Codec:QualityRange dictionary
quality = {
"daala": [3,5,7,11,16,25,37,55,81,122,181,270,400],
"x264":
range(1,52,5),
"x265":
range(5,52,5),
"x265-rt":
range(5,52,5),
"vp8":
range(1,64,4),
"vp9":
range(1,64,4)
}

#declare the lists we will need
free_slots = []
taken_slots = []

work_items = []
work_done = []

machines = []

#load all the different sets and their filenames
video_sets_f = open('sets.json','r')
video_sets = json.load(video_sets_f)

parser = argparse.ArgumentParser(description='Collect RD curve data.')
parser.add_argument('set',metavar='Video set name')
parser.add_argument('-codec',default='daala')
parser.add_argument('-prefix',default='.')
args = parser.parse_args()

#check we have the codec in our codec-qualities dictionary
if args.codec not in quality:
    print(GetTime(),'Invalid codec. Valid codecs are:')
    for q in quality:
        print(GetTime(),q)
    sys.exit(1)

#check we have the set name in our sets-filenames dictionary
if args.set not in video_sets:
    print(GetTime(),'Specified invalid set '+args.set+'. Available sets are:')
    for video_set in video_sets:
        print(GetTime(),video_set)
    sys.exit(1)

total_num_of_jobs = len(video_sets[args.set]) * len(quality[args.codec])

#a logging message just to get the regex progress bar on the AWCY site started...
print(GetTime(),'0 out of',total_num_of_jobs,'finished.')

#how many AWS instances do we want to spin up?
#The assumption is each machine can deal with 32 threads,
#so up to 32 jobs, use 1 machine, then up to 64 use 2, etc...
num_instances_to_use = (31 + total_num_of_jobs) / 32

#...but lock AWS to a max number of instances
max_num_instances_to_use = 8

if num_instances_to_use > max_num_instances_to_use:
  print(GetTime(),'Ideally, we should use',num_instances_to_use,
    'AWS instances, but the max is',max_num_instances_to_use,'.')
  num_instances_to_use = max_num_instances_to_use

#connect to AWS
ec2 = boto.ec2.connect_to_region('us-west-2');
autoscale = boto.ec2.autoscale.AutoScaleConnection();

#how many machines are currently running?
group = autoscale.get_all_groups(names=['Daala'])[0]
num_instances = len(group.instances)
print(GetTime(),'Number of instances online:',len(group.instances))

#switch on more machines if we need them
if num_instances < num_instances_to_use:
    print(GetTime(),'Launching instances...')
    autoscale.set_desired_capacity('Daala',num_instances_to_use)

    #tell us status every few seconds
    group = None
    while num_instances < num_instances_to_use:
        group = autoscale.get_all_groups(names=['Daala'])[0]
        num_instances = len(group.instances)
        print(GetTime(),'Number of instances online:',len(group.instances))
        sleep(3)

#grab instance IDs
instance_ids = [i.instance_id for i in group.instances]
print(GetTime(),"These instances are online:",instance_ids)

if 1:
    instances = ec2.get_only_instances(instance_ids)
    for instance in instances:
        print(GetTime(),'Waiting for instance',instance.id,'to boot...')
        while 1:
            instance.update()
            if instance.state == 'running':
                print(GetTime(),instance.id,'is running!')
                break
            sleep(3)
    for instance_id in instance_ids:
        print(GetTime(),'Waiting for instance',instance_id,'to report OK...')
        while 1:
            statuses = ec2.get_all_instance_status([instance_id])
            if len(statuses) < 1:
                sleep(3)
                continue
            status = statuses[0]
            if status.instance_status.status == 'ok':
                print(GetTime(),instance.id,'reported OK!')
                break
            sleep(3)

    #make a list of our instances' IP addresses
    for instance in instances:
        machines.append(Machine(instance.ip_address))

    #set up our instances and their free job slots
    for machine in machines:
        machine.setup()

    #by doing the machines in the inner loop,
    #we end up with heavy jobs split across machines better
    for i in range(0,32):
        for machine in machines:
            free_slots.append(Slot(machine))

#Make a list of the bits of work we need to do.
#We pack the stack ordered by filesize ASC, quality ASC (aka. -v DESC)
#so we pop the hardest encodes first,
#for more efficient use of the AWS machines' time.
for filename in video_sets[args.set]:
    for q in sorted(quality[args.codec], reverse = True):
        work = Work()
        work.quality = q
        work.set = args.set
        work.filename = filename
        work_items.append(work)

if len(free_slots) < 1:
    print(GetTime(),'All AWS machines are down.')
    sys.exit(1)

retries = 0
max_retries = 10

while(1):
    for slot in taken_slots:
        if slot.busy() == False:
            slot.gather()
            if slot.work.failed == False:
                work_done.append(slot.work)
                print(GetTime(),len(work_done),'out of',total_num_of_jobs,'finished.')
            elif retries >= max_retries:
                break
            else:
                retries = retries + 1
                print(GetTime(),'Retrying work...',retries,'of',max_retries,'retries.')
                work_items.append(slot.work)
            taken_slots.remove(slot)
            free_slots.append(slot)

    #have we finished all the work?
    if len(work_items) == 0:
        if len(taken_slots) == 0:
            print(GetTime(),'All work finished.')
            break
    elif retries >= max_retries:
        print(GetTime(),'Max number of failed retries reached!')
        break
    else:
        if len(free_slots) != 0:
            slot = free_slots.pop()
            work = work_items.pop()
            threading.Thread(slot.execute(work))
            taken_slots.append(slot)
    sleep(0.02)

work_done.sort(key=lambda work: work.quality)

print(GetTime(),'Logging results...')
for work in work_done:
    work.parse()
    if not work.failed:
        f = open((args.prefix+'/'+work.filename+'-'+args.codec+'.out').encode('utf-8'),'a')
        f.write(str(work.quality)+' ')
        f.write(str(work.pixels)+' ')
        f.write(str(work.size)+' ')
        f.write(str(work.metric['psnr'][0])+' ')
        f.write(str(work.metric['psnrhvs'][0])+' ')
        f.write(str(work.metric['ssim'][0])+' ')
        f.write(str(work.metric['fastssim'][0])+' ')
        f.write('\n')
        f.close()

subprocess.call('OUTPUT="'+args.prefix+'/'+'total" "'+daala_root+'/tools/rd_average.sh" "'+args.prefix+'/*.out"',
    shell=True);

print(GetTime(),'Done!')
