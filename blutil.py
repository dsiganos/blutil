#!/usr/bin/env python3
"""
blutil.py is a command line tool for programming BL-600SA "SmartBASIC" devices.

See http://projectgus.com/2014/03/laird-bl600-modules for more details

Copyright (C)2014 Angus Gratton, released under BSD license as per the LICENSE file.
"""

import argparse, serial, time, subprocess, sys, os, re, tempfile, requests, json

parser = argparse.ArgumentParser(
    description='Perform various operations with a BL600 module. The -m option can only be used instead of -p when compiling using the -c flag. -p on the other hand is compatible with all other argument choices.')
device_arg = parser.add_mutually_exclusive_group(required=True)
device_arg.add_argument('-p', '--port', help="Serial port to connect to")
device_arg.add_argument('-m', '--model',
                        help="Specify (instead of detecting) the model number, see command output for example model string")
parser.add_argument('-b', '--baud', type=int, default=9600, help="Baud rate for connection")
parser.add_argument('--no-dtr', action="store_true", help="Don't toggle the DTR line as a reset")
cmd_arg = parser.add_mutually_exclusive_group(required=True)
cmd_arg.add_argument('-c', '--compile', help="Compile specified smartBasic file to a .uwc file.", metavar="BASICFILE")
cmd_arg.add_argument('-l', '--load',
                     help="Upload specified smartBasic file to BL600 (if argument is a .sb file it will be compiled first.)",
                     metavar="FILE")
cmd_arg.add_argument('-r', '--run',
                     help="Execute specified smartBasic file on BL600 (if argument is a .sb file it will be compiled and uploaded first, if argument is a .uwc file it will be uploaded first.)",
                     metavar="FILE")
cmd_arg.add_argument('--ls', action="store_true", help="List all files uploaded to the BL600")
cmd_arg.add_argument('--rm', metavar="FILE", help="Remove specified file from the BL600")
cmd_arg.add_argument('--format', action="store_true", help="Erase all stored files from the BL600")
cmd_arg.add_argument('--at', help="Run an AT command")
cmd_arg.add_argument('--listen', action="store_true",
                     help="Listen over serial for incoming messages, e.g. from print statements in a running program")


def to_uwc(filepath):
    parts = os.path.splitext(filepath)
    return "%s.uwc" % parts[0]


class RuntimeError(Exception):
    pass


class BLDevice(object):
    def __init__(self, args):
        if args.model is None:
            self.port = serial.Serial(args.port, args.baud, timeout=2)

    def writecmd(self, args, expect_response=True, timeout=0.5):
        command = f"AT{'' if args.startswith('+') else ' '}{args}\r"
        self.port.write(bytearray(command, "ascii"))
        if not expect_response:
            return
        response = b''
        start = time.time()
        while not response.endswith(b"00\r") and time.time() < start + timeout:
            response += self.port.read(1)
        if response.endswith(b"00\r"):
            return str(response, "ascii")[:-3].strip()
        else:
            if len(response) == 0:
                raise RuntimeError(
                    f"Got no response to command {repr(command)}. Not connected or not in interactive mode?")
            elif len(response) > 4 and response[0:4] == b'\n01\t':
                errorcode = str(response[4:].decode())[:-1]
                raise RuntimeError("BL600 returned error %s: %s" % (errorcode, get_errordesc(errorcode)))
            else:
                raise RuntimeError("Got unexpected/error response to command 'AT%s': %s" % (args, response))

    def read_param(self, param):
        return self.writecmd("I %d" % param).split("\t")[-1]

    def detect_model(self):
        model = self.read_param(0)
        revision = self.read_param(13)
        print("Detected model %s %s" % (model, revision))
        self.model = "%s_%s" % (model, revision.replace(" ", "_"))

    def compile(self, filepath):
        blutil_dir = os.path.dirname(sys.argv[0])
        compiler = os.path.join(blutil_dir, "XComp_%s.exe" % (self.model,))

        filepath = os.path.expanduser(filepath)
        filepath = os.path.abspath(filepath)
        if not os.path.exists(filepath):
            raise RuntimeError("File '%s' not found" % filepath)
        if not os.path.exists(compiler):
            return self.online_compile(filepath)
        print("Compiling %s with %s..." % (filepath, os.path.basename(compiler)))
        args = [compiler, filepath]
        if os.name != 'nt':
            args = ["wine"] + args
        ret = subprocess.call(args, stdin=None, stdout=sys.stdout, stderr=sys.stderr, shell=False)
        if ret != 0:
            raise RuntimeError("Compilation failed")
        print("Compilation success")

    def online_compile(self, filepath):
        print('Using the online compiler')
        url = 'http://uwterminalx.no-ip.org/xcompile.php?JSON=1'

        with open('devices.json','r') as f:
            devices = json.load(f)

        devices = dict(devices)

        model = self.read_param(0)
        firmware_code = self.read_param(3)
        model_firmwares = dict((firmware[1],firmware[0]) for firmware in devices[model])
        firmware_index = model_firmwares[firmware_code]
        payload = {'file_XComp': f"{model}_{firmware_index}"}

        with open(filepath, 'r') as f:
            file_data = f.read()
            file_dir = os.path.dirname(filepath)
            file_data = self.do_include(file_data, file_dir)
            file_data = file_data.encode('utf-8')

        with open('temp.sb','wb') as f:
            f.write(file_data)

        files = {'file_sB': (os.path.basename(filepath), file_data, 'application/octet-stream')}
        response = requests.post(url, data=payload, files=files)
        if response.status_code // 100 != 2:
            error = json.loads(response.content, encoding=response.encoding)
            if error['Result'] == '-9':
                raise RuntimeError(f"{error['Error']}:\n{error['Description']}")
            raise RuntimeError(f"Online compiler error code {error['Result']}: {error['Error']}")

        f = open(to_uwc(filepath), 'wb')
        f.write(response.content)
        f.close()

    def upload(self, filepath):
        filepath = os.path.expanduser(filepath)
        filepath = os.path.abspath(filepath)

        parts = os.path.splitext(filepath)
        if parts[1] != ".uwc":  # compiled files have .uwc extension
            filepath = "%s.uwc" % (parts[0],)
        devicename = get_devicename(filepath)
        print("Uploading %s as %s" % (filepath, devicename))
        self.writecmd('+DEL "%s" +' % devicename)
        self.writecmd('+FOW "%s"' % devicename)
        with open(filepath, "rb") as f:
            for line in chunks(f, 16):
                row = "".join(["%02x" % x for x in line])
                self.writecmd('+FWRH "%s"' % row)
        self.writecmd('+FCL')
        print("Upload success")

    def run(self, filepath):
        devicename = get_devicename(filepath)
        self.writecmd('')  # check is responding at all
        print("Running %s..." % devicename)
        self.writecmd('+RUN "%s"' % devicename, expect_response=False)
        output = self.port.read(1024)
        if len(output):
            if len(output) >= 3 and output[-3:] == b'00\r':
                if len(output) > 3:
                    print("Output:\n%s" % output[:-3].decode())
                print("Program completed successfully.")
            elif len(output) > 4 and output[0:4] == b'\n01\t':
                errorcode = str(output[4:].decode())[:-1]
                print("Error %s: %s" % (errorcode, get_errordesc(errorcode)))
            elif output != b'\n00':
                print("Immediate output:\n%s" % output.decode('utf-8'))
        else:
            print("No immediate output, program probably running...")

    def list(self):
        print("Listing files...")
        output = self.writecmd('+DIR')
        print(output)

    def delete(self, filename):
        filename = get_devicename(filename)
        print("Removing %s..." % filename)
        self.writecmd('+DEL "%s"' % filename)
        print("Deleted.")

    def format(self):
        print("Formatting BL600...")
        self.writecmd('Z', timeout=5)
        self.writecmd('')
        self.writecmd('&F 1', expect_response=False)
        time.sleep(0.2)
        self.port.read(1024)  # discard anything
        print("Format complete. Reconnecting...")
        self.writecmd('')

    def do_include(self, file, dirname):
        pattern = re.compile(r'^#include\s+"(.*)"$', re.MULTILINE)
        match = pattern.search(file)
        if match is None:
            return file

        include_path = os.path.join(dirname, match.group(1))
        include_path = os.path.abspath(include_path)

        if not os.path.exists(include_path):
            raise RuntimeError(f"Included file {include_path} does not exist")

        with open(include_path, 'r') as include_file:
            file_data = include_file.read()
            include_dirname = os.path.dirname(include_path)
            file_data = self.do_include(file_data, include_dirname)
            file = f"{file[:match.start()]}\n{file_data}\n{file[match.end():]}"

        file = self.do_include(file, dirname)

        # the online compiler doesn't allow the string #include anywhere
        # UwTerminalX does this replace too
        file = file.replace('#include', "")
        return file

    def listen(self):
        try:
            while True:
                print(self.port.read(1).decode(), end='')
        except KeyboardInterrupt:
            print('\n')


def chunks(somefile, chunklen):
    while True:
        chunk = somefile.read(chunklen)
        if len(chunk) == 0:
            return
        yield chunk


def get_devicename(filepath):
    """ Given a file path, find an acceptable name on the BL filesystem """
    filename = os.path.split(filepath)[1]
    filename = filename.split('.')[0]
    return re.sub(r'[:*?"<>|]', "", filename)[:24]


def test_wine():
    """ Check the wine installation is OK """
    try:
        with tempfile.TemporaryFile() as blackhole:
            ret = subprocess.call(["wine", "--version"], stdin=None, stdout=blackhole, stderr=None, shell=False)
        if ret != 0:
            raise RuntimeError("Wine returned error code" % ret)
    except Exception as e:
        print("Wine execution failed. %s. Make sure wine is in your path and properly configured" % e)
        sys.exit(2)


def get_errordesc(code):
    """ Go through file with list of error codes to find description """
    blutil_dir = os.path.dirname(sys.argv[0])
    with open(os.path.join(blutil_dir, 'codes.csv')) as f:
        for line in f:
            if str(eval("0x" + code)) in line:
                return line.split('"')[1]
                break
        return "(no description available)"

def main():
    if os.name != 'nt':
        test_wine()
    args = parser.parse_args()
    device = BLDevice(args)

    # Preload any .sb or .uwc file
    if args.run is not None:
        split = os.path.splitext(args.run)
        if split[1] == ".uwc" or split[1] == ".sb":
            args.load = args.run

    # Precompile any .sb file
    if args.load is not None:
        split = os.path.splitext(args.load)
        if split[1] == ".sb":
            args.compile = args.load

    ops = []
    if args.compile:
        ops += ["compile"]
    if args.load:
        ops += ["load"]
    if args.run:
        ops += ["run"]

    if (args.load or args.run or args.rm or args.ls or args.format or args.model is None) and not args.no_dtr:
        print("Resetting board via DTR...")
        device.port.setDTR(False)
        time.sleep(0.1)
        device.port.setDTR(True)

    if args.model is not None:
        device.model = args.model.replace(" ", "_")
    elif args.compile:
        device.detect_model()

    if len(ops) > 0:
        print("Performing %s for %s..." % (", ".join(ops), sys.argv[-1]))

    if args.ls:
        device.list()
    if args.rm:
        device.delete(args.rm)
    if args.format:
        device.format()
    if args.compile:
        device.compile(args.compile)
    if args.load:
        device.upload(args.load)
    if args.run:
        device.run(args.run)
    if args.at:
        print(device.writecmd(args.at, timeout=5))
        print("Command completed")
    if args.listen:
        device.listen()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        print(e)
        sys.exit(2)
    except serial.SerialException as e:
        print(e)
        sys.exit(3)
