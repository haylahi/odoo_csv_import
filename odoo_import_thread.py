'''
Created on 16 mai 2014

@author: openerp
'''

from lib.csv_reader import UnicodeReader, UnicodeWriter
from xmlrpclib import Fault
from lib import conf_lib
import argparse
import sys
import threading
import csv
import traceback
from time import time
from copy import deepcopy

csv.field_size_limit(sys.maxsize)

from itertools import islice, chain

def batch(iterable, size):
    sourceiter = iter(iterable)
    while True:
        batchiter = islice(sourceiter, size)
        yield chain([batchiter.next()], batchiter)

class rpc_thread(threading.Thread):

    def __init__(self, semaphore, max_thread_semaphore, model, header, data_lines, model_data, writer, batch_number=0, batch_size=20):
        threading.Thread.__init__(self)
        self.semaphore = semaphore
        self.max_thread_semaphore = max_thread_semaphore
        self.model = model
        self.header = header
        self.lines = [deepcopy(l) for l in data_lines]
        self.batch_size = batch_size

        self.model_data = model_data

        self.writer = writer

        self.batch_number = batch_number

    def _extract_xml_ids(self, lines):
        id_index = self.header.index('id')
        module_list = set()
        xml_ids = []
        for l in lines:
            xml_id = l[id_index].split('.')
            if len(xml_id) == 2:
                xml_ids.append(xml_id[1])
                module_list.add(xml_id[0])
            else:
                xml_ids.append(xml_id[0])
        module_list = list(module_list)
        return xml_ids, module_list

    def single_batch_run(self, lines, xml_ids, module_list, sub_batch_number):
        success = False

        st = time()
        try:
            if self._send_rpc(lines, sub_batch_number):
                success = self.check_result(xml_ids, module_list)
        except Fault as e:
            print "Line", i, "Failed"
            print e.faultString
        except ValueError:
            print "Line", i, "Failed"
        except Exception as e:
            print "Unknown Problem"
            exc_type, exc_value, exc_traceback = sys.exc_info()
            traceback.print_tb(exc_traceback, file=sys.stdout)
            print >> sys.stderr, exc_type
            print >> sys.stderr, exc_value



        if not success:
            self.writer.writerows(self.lines)

        print "time for batch %s" % self.batch_number, '-', sub_batch_number * self.batch_size, 'on', len(self.lines), ":", time() - st


    def run(self):
        lines = []
        i = 0
        self.semaphore.acquire()
        print "Start Batch [%s]" % self.batch_number
        for lines in batch(self.lines, self.batch_size):
            lines = [l for l in lines]
            xml_ids, module_list = self._extract_xml_ids(lines)
            self.single_batch_run(lines, xml_ids, module_list, i)
            i += 1
        self.semaphore.release()
        self.max_thread_semaphore.release()

    def _send_rpc(self, lines, sub_batch_number):
        #TODO context in configuration context={'create_product_variant' : True}
        res = self.model.load(self.header, lines, context={'tracking_disable' : True, 'create_product_variant' : True, 'check_move_validity' : False})
        if res['messages']:
            for msg in res['messages']:
                print >> sys.stderr, 'line ' + str(self.batch_number) + ', ' + str(sub_batch_number)
                print >> sys.stderr, msg
                print >> sys.stderr, lines[msg['record']]
            return False

        return True

    def check_result(self, xml_ids, module_list):
        domain = [['name', 'in', xml_ids],
                  ['model', '=', self.model.model_name]]
        if module_list:
            domain.append(['module', 'in', module_list])
        object_ids = self.model_data.search(domain)
        return len(object_ids) == len(xml_ids)

def do_not_split(split, previous_split_value, split_index, line):
    if not split: # If no split no need to continue
        return False

    split_value = line[split_index]
    if split_value != previous_split_value: #Different Value no need to split
        return False

    return True


parser = argparse.ArgumentParser(description='Import data in batch and in parallel')
parser.add_argument('-c', '--config', dest='config', default="conf/connection.conf", help='Configuration File', required = True)
parser.add_argument('--file', dest='filename', help='File to import', required = True)
parser.add_argument('--model', dest='model', help='Model to import, if auto try to guess the model from the filename', required = True)
parser.add_argument('--worker', dest='worker', default=1, help='Number of simultaneous connection')
parser.add_argument('--size', dest='batch_size', default=10, help='Number of line to import per connection')
parser.add_argument('--skip', dest='skip', default=0, help='Number of line to skip')
parser.add_argument('-f', '--fail',action='store_true', dest="fail", help='Fail mode')
parser.add_argument('-s', '--sep', dest="seprator", default=";", help='Fail mode')
parser.add_argument('--split', dest='split', help='Keep batch same value of the field in the same batch')



if len(sys.argv) == 1:
    sys.exit(parser.print_help())

args = parser.parse_args()
config_file = args.config
file_csv = args.filename
batch_size = int(args.batch_size)
model = args.model
fail_file = file_csv + ".fail"
max_connection = int(args.worker)
separator = args.seprator
split = False

if args.fail:
    file_csv = fail_file
    fail_file = fail_file + ".bis"
    batch_size = 1
    max_connection = 1

if args.split:
    split = args.split


semaphore = threading.BoundedSemaphore(int(max_connection))
max_thread_semaphore = threading.BoundedSemaphore(int(max_connection) * 10)
file_ref = open(file_csv, 'r')
reader = UnicodeReader(file_ref, delimiter=separator)
print 'open', file_csv
print >> sys.stderr, 'open', file_csv


connection = conf_lib.get_server_connection(config_file)
object_registry = connection.get_model(model)
ir_model_registry = connection.get_model('ir.model.data')

header = reader.next()
header_len = 0

for head in header:
    if head:
        header_len += 1
    else:
        break

header = header[:header_len]

try:
    id_index = header.index('id')
except ValueError as ve:
    print "No External Id (id) column defined, please add one"
    raise ve


print "Skipping %s lines" % args.skip
i = 1
while i <= int(args.skip):
    reader.next()
    i+= 1;

file_result = open(fail_file, "wb")

c = UnicodeWriter(file_result, delimiter=separator, quoting=csv.QUOTE_ALL)
c.writerow(header)
file_result.flush()
thread_list = []
st = time()

data = [l for l in reader]
split_index = 0
if split:
    try:
        split_index = header.index(split)
    except ValueError as ve:
        print "column %s not defined" % split
        raise ve
    data = sorted(data, key=lambda d: d[split_index])

i = 0
previous_split_value = False
while  i < len(data):
    lines = []

    j = 0
    while i < len(data) and (j < batch_size or do_not_split(split, previous_split_value, split_index, data[i])):
        line = data[i][:header_len]
        lines.append(line)
        previous_split_value = line[split_index]
        j += 1
        i += 1
    batch_number = previous_split_value and "[%s] - [%s]" % (len(thread_list), previous_split_value) or "[%s]" % len(thread_list)
    max_thread_semaphore.acquire()
    th = rpc_thread(semaphore, max_thread_semaphore, object_registry, header, lines, ir_model_registry, c, batch_number, batch_size)
    thread_list.append(th)
    th.start()

print "Total Batch", len(thread_list)
for t in thread_list:
    t.join()
file_result.close()

print "total time", time() - st