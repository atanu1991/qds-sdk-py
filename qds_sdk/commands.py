""" 
The commands module contains the base definition for
a generic Qubole command and the implementation of all 
the specific commands
"""

from qubole import Qubole
from resource import Resource
from exception import ParseError
from account import Account
from qds_sdk.util import GentleOptionParser
from qds_sdk.util import OptionParsingError
from qds_sdk.util import OptionParsingExit

import boto
import boto.s3.connection

import time
import logging
import sys
import re
import os

log = logging.getLogger("qds_commands")

#Pattern matcher for s3 patho
_URI_RE = re.compile(r's3://([^/]+)/?(.*)')

class Command(Resource):

    """
    qds_sdk.Command is the base Qubole command class. Different types of Qubole
    commands can subclass this.
    """

    """ all commands use the /commands endpoint"""
    rest_entity_path="commands"

    @staticmethod
    def is_done(status):
        """
        Does the status represent a completed command
        Args:
            ``status``: a status string

        Returns:
            True/False
        """
        return (status == "cancelled" or status == "done" or status == "error")
    

    @classmethod
    def create(cls, **kwargs):
        """
        Create a command object by issuing a POST request to the /command endpoint
        Note - this does not wait for the command to complete

        Args:
            `\**kwargs` - keyword arguments specific to command type

        Returns:
            Command object
        """

        conn=Qubole.agent()
        if kwargs.get('command_type') is None:
            kwargs['command_type'] = cls.__name__

        return cls(conn.post(cls.rest_entity_path, data=kwargs))


    @classmethod
    def run(cls, **kwargs):
        """
        Create a command object by issuing a POST request to the /command endpoint
        Waits until the command is complete. Repeatedly polls to check status

        Args:
            `\**kwargs` - keyword arguments specific to command type

        Returns:
            Command object
        """
        cmd = cls.create(**kwargs)
        while not Command.is_done(cmd.status):
            time.sleep(Qubole.poll_interval)
            cmd = cls.find(cmd.id)

        return cmd

    @classmethod
    def cancel_id(cls, id):
        """
        Cancels command denoted by this id

        Args:
            `id` - command id
        """
        conn=Qubole.agent()
        data={"status":"kill"}
        conn.put(cls.element_path(id), data)
        

    def cancel(self):
        """
        Cancels command represented by this object
        """
        self.__class__.cancel_id(self.id)


    def get_log(self):
        """
        Fetches log for the command represented by this object

        Returns:
            The log as a string
        """
        log_path = self.meta_data['logs_resource']
        conn=Qubole.agent()
        r=conn.get_raw(log_path)
        return r.text

    def get_results(self):
        """
        Fetches the result for the command represented by this object

        Returns:
            The result as a string
        """
        result_path = self.meta_data['results_resource']
        
        conn=Qubole.agent()
        r = conn.get(result_path , {'inline': False})
        if r.get('inline'):
            return r['results'] 
        else:
            # Making default path /tmp/Downloads/<query-id>.
            # An option must be provided for the user to enter the path
            my_path = "/tmp/Downloads/"+str(self.id)
            
            if not os.path.exists(my_path):
                os.makedirs(my_path)
                
            accnt_obj = Qubole.get_Account()
            
            acc_key = accnt_obj.get_access_key()
            secret_key = accnt_obj.get_secret_key()
            
            #Establish connection to s3    
            conn = boto.connect_s3(
                aws_access_key_id=acc_key,
                aws_secret_access_key=secret_key,
                #is_secure=False,               # uncomment if you are not using ssl
                #calling_format = boto.s3.connection.OrdinaryCallingFormat(),
                )
            
            for s3_path in  r['result_location']:
                _download_to_local(conn, s3_path, my_path)
             
            log.info("Files successfully downloaded to %s path" % my_path)    
            return "\nFind all the downloaded files in %s location" % (my_path)
            
def _download_to_local(conn, path, my_path):
    '''
    Copies the contents of S3 key instance with name as key_name into the path specified by path.
    Path defaults to the current path
    Returns the name of the new file created
    
    @param conn_dest: S3 connection object for path from where the file is to be retrieved
    @type conn_dest: S3 Connection object
    @param path: The path from where the file is to be downloaded or directory name
    @type path: String
    
    '''
    #Do we need to display a progress bar?
    def _callback(downloaded,  total):
        '''
        Call function for upload.
        @param key_name: File size already downloaded
        @type key_name: int
        @param key_prefix: Total file size to be downloaded
        @type key_prefix: int
        '''
        if total is 0:
            return
        progress = downloaded*100/total
        print ('\r[{0}] {1}%'.format('#'*progress, progress)),
        sys.stdout.flush()
        
    
    m = _URI_RE.match(path)     
    #It is assumed path is always valid.
    bucket_name = m.group(1)
        
    if path.endswith('/') is False:
        #It is a file
        key_name = m.group(2)
        
        bucket = conn.get_bucket(bucket_name)
        key_instance = bucket.get_key(key_name)
        
        tmp_file_name = key_name[key_name.rfind('/')+1:]
        fp = open(my_path + '/' + tmp_file_name, "w+")
        
        key_instance.get_contents_to_file(fp, None, _callback)
        fp.close()
        print '\n'
        
    else:
        #It is a folder
        key_prefix = m.group(2)
        bucket = conn.get_bucket(bucket_name)
        bucket_paths = bucket.list(key_prefix)
        
        for each_file in bucket_paths:
            each_file_name = each_file.name
            
            #Eliminate _tmp_ files which ends with $folder$
            if each_file_name.endswith('$folder$'):
                continue
                
            #Strip the prefix from each_file_name
            tmp_file_name_with_path = each_file_name[len(key_prefix):]
            tmp_file_name = tmp_file_name_with_path[tmp_file_name_with_path.rfind('/')+1:]
            tmp_dir_name = tmp_file_name_with_path[:tmp_file_name_with_path.rfind('/')]
            tmp_dir_name = my_path + '/' + tmp_dir_name
            
            if not os.path.exists(tmp_dir_name):
                os.makedirs(tmp_dir_name)  
            fp = open(tmp_dir_name + '/' + tmp_file_name, "w+")
        
            each_file.get_contents_to_file(fp, None, _callback)
            fp.close()
            print '\n'

class HiveCommand(Command):

    usage = ("hivecmd <--query query-string | --script_location location-string>"
             " [--macros <expressions-to-expand-macros>]"
             " [--sample_size <sample-bytes-to-run-query-on]")
               

    optparser = GentleOptionParser(usage=usage)
    optparser.add_option("--query", dest="query", help="query string")

    optparser.add_option("--script_location", dest="script_location", 
                         help="Path where hive query to run is stored")

    optparser.add_option("--macros", dest="macros", 
                         help="expressions to expand macros used in query")

    optparser.add_option("--sample_size", dest="sample_size", 
                         help="size of sample in bytes on which to run query")


    @classmethod
    def parse(cls, args):
        """
        Parse command line arguments to construct a dictionary of command
        parameters that can be used to create a command

        Args:
            `args` - sequence of arguments

        Returns:
            Dictionary that can be used in create method

        Raises:
            ParseError: when the arguments are not correct
        """

        try:
            (options, args) = cls.optparser.parse_args(args)
            if options.query is None and options.script_location is None:
                raise ParseError("One of query or script location"
                                 " must be specified", cls.usage)
        except OptionParsingError as e:
            raise ParseError(e.msg, cls.usage)
        except OptionParsingExit as e:
            return None
        
        return vars(options)

class HadoopCommand(Command):
    subcmdlist = ["jar", "s3distcp", "streaming"]
    usage = "hadoopcmd <%s> <arg1> [arg2] ..." % " | ".join(subcmdlist)
    
    @classmethod
    def parse(cls, args):
        """
        Parse command line arguments to construct a dictionary of command
        parameters that can be used to create a command

        Args:
            `args` - sequence of arguments

        Returns:
            Dictionary that can be used in create method

        Raises:
            ParseError: when the arguments are not correct
        """
        parsed = {}
        
        if len(args) >= 1 and args[0] == "-h":
            sys.stderr.write(cls.usage + "\n")
            return None

        if len(args) < 2:
            raise ParseError("Need at least two arguments", cls.usage)
        
        subcmd = args.pop(0)
        if subcmd not in cls.subcmdlist:
            raise ParseError("First argument must be one of <%s>" % 
                             "|".join(cls.subcmdlist))

        parsed["sub_command"] = subcmd
        parsed["sub_command_args"] = " ".join("'" + a + "'" for a in args)
        
        return parsed

    pass

class PigCommand(Command):
    @classmethod
    def parse(cls, args):
        raise ParseError("pigcmd not implemented yet", "")
    pass

class DbImportCommand(Command):
    @classmethod
    def parse(cls, args):
        raise ParseError("dbimport command not implemented yet", "")
    pass
