import click
import sys
import os
import subprocess
import shlex
from pwncli.utils.config import try_get_config_data_by_key
from pwn import context, which, ELF, process, remote, atexit
from pwncli.cli import pass_environ, _Inner_Dict, _set_filename

def _in_tmux():
    return bool('TMUX' in os.environ and which('tmux'))

def _in_wsl():
    if os.path.exists('/proc/sys/kernel/osrelease'):
        with open('/proc/sys/kernel/osrelease', 'rb') as f:
            is_in_wsl = b'icrosoft' in f.read()
        if is_in_wsl and which('wsl.exe') and which("wt.exe"):
            return True
    return False


def _set_gdb_type(pwncli_path, gdb_type):
    if gdb_type == 'auto':
        return None
    dirname = os.path.join(pwncli_path, "conf")

    if gdb_type == "pwndbg":
        gdbfile = ".gdbinit-pwndbg"
    elif gdb_type == "gef":
        gdbfile = ".gdbinit-gef"
    else:
        gdbfile = ".gdbinit-peda"

    fullpath = os.path.join(dirname, gdbfile)
    targpath = os.path.expanduser("~/.gdbinit")
    oldcontent = b""
    with open(targpath, "rb") as f:
        oldcontent = f.read()
    with open(targpath, "wb") as f:
        with open(fullpath, "rb") as f2:
            f.write(f2.read())
    return oldcontent, targpath

_arch_usr_map = {
    "arm": ("qemu-arm", "/usr/arm-linux-gnueabi"),
    "armhf": ("qemu-arm", "/usr/arm-linux-gnueabihf"),
    "aarch64": ("qemu-aarch64", "/usr/aarch64-linux-gnu"),
    "mips": ("qemu-mips", "/usr/mips-linux-gnu"),
    "mips64": ("qemu-mips64", "/usr/mips64-linux-gnuabi64"),
    "mipsn32": ("qemu-mipsn32", "/usr/mips64-linux-gnuabin32"),
    "mips64el": ("qemu-mips64el", "/usr/mips64el-linux-gnuabi64"),
    "mipsn32el": ("qemu-mipsn32el", "/usr/mips64el-linux-gnuabin32"),
    "mipsel": ("qemu-mipsel", "/usr/mipsel-linux-gnu")
}

def __recover(f, c):
    with open(f, "wb") as f2:
        f2.write(c)


def __parse_gdb_examine(ctx, args):
    gdb_examine = ""
    for gb in args.gdb_breakpoint:
        if not gb:
            continue
        if gb.startswith('0x') or gb.isdecimal() or all(c in string.hexdigits for c in gb):
            gdb_examine += " -ex 'b *{}'".format(gb)
        else:
            gdb_examine += " -ex 'b {}'".format(gb)

    if args.gdb_script:
        script = args.gdb_script
        if isinstance(script, str):
            for x in script.split(";"):
                x = x.strip()
                if x:
                    gdb_examine += " -ex '{}'".format(x)
        elif os.path.isfile(script):
            gdb_examine += " -x {}".format(script)
        else:
            ctx.abort("qemu-command --> Invalid gdb_script, please check.")
    return gdb_examine

def __debug_mode(ctx, args:_Inner_Dict):
    if args.tmux or args.wsl or args.gnome:
        args['use_gdb'] = True
        if not which("gdb-multiarch"):
            ctx.abort("qemu-command --> Please install gdb-multiarch first.")

    # if launch_script is specified
    if args.launch_script:
        # TODO
        process_args = []
        args.port = 1234 # ????
    else:
        process_args = []
        # get file arch info
        arch = context.binary.arch
        process_args.append(_arch_usr_map[arch][0])
        if not context.binary.statically_linked and b"armhf" in context.binary.linker:
            arch = "armhf"
        
        if args.static:
            process_args[0] += "-static"
        # 
        if args.use_gdb:
            process_args.append("-g")
            if not args.ip:
                args.ip = "127.0.0.1"
                ctx.vlog("qemu-command --> Set default gdb listen ip {}.".format(args.ip))
            if not args.port:
                args.port = 1234
                ctx.vlog("qemu-command --> Set default gdb listen port {}.".format(args.port))
            process_args.append(str(args.port))

        process_args.append("-L")
        if not args.lib:
            args.lib = _arch_usr_map[arch][1]
            ctx.vlog("qemu-command --> Set default lib path: {}.".format(args.lib))
        process_args.append(args.lib)
    
    # set process
    process_args.append(args.filename)
    ctx.gift['io'] = process(process_args)
    
    if not args.use_gdb:
        return
    
    gdbs = _set_gdb_type(ctx.pwncli_path, args.gdb_type)
    if gdbs:
        atexit.register(__recover, gdbs[1], gdbs[0])
    
    if args.tmux and not _in_tmux():
        ctx.abort("qemu-command --> Not in tmux")
    
    if args.wsl and not _in_wsl():
        ctx.abort("qemu-command --> Not in wsl")
    
    if args.gnome and not which("gnome-terminal"):
        ctx.abort("qemu-command --> No gnome-terminal")

    if args.tmux:
        cmd = "tmux splitw -h" 
    elif args.wsl:
        cmd = "cmd.exe /c start wt.exe wsl.exe bash -c"
    elif args.gnome:
        cmd = "gnome-terminal -- sh -c"
    
    # parse gdbsecipt and breakpoints 
    gdb_examine = __parse_gdb_examine(ctx, args)
    cmd += " \"gdb-multiarch {} -ex 'target remote {}:{}' {}\"".format(args.filename, args.ip, args.port, gdb_examine)

    # os.system(cmd)
    ctx.vlog("qemu-command --> Exec cmd: {}".format(cmd))
    cur_p = subprocess.Popen(shlex.split(cmd))
    atexit.register(func=lambda x: x.kill(), x=cur_p)
    

def __remote_mode(ctx, args:_Inner_Dict):
    if not args.ip:
        ip = try_get_config_data_by_key(ctx.config_data, "remote", "ip")
        if not ip:
            ctx.abort("qemu-command --> Please set ip from cli or config file.")
        args.ip = ip
    
    ctx.gift['io'] = remote(args.ip, args.port)
    ctx._log("connect {} port {} success!".format(args.ip, args.port))


def __process_args(ctx, args:_Inner_Dict):
    # parse
    if not ctx.gift.filename:
        _set_filename(ctx, args['filename'])
    if args['target']:
        if ":" not in args['target']:
            ctx.abort("qemu-command --> Target wrong, format is ip:port")
        if args.ip or args.port:
            ctx.abort("qemu-command --> Cannot specify ip and port again when target is not None.")
        ip, port = args['target'].strip().split(":")
        args.ip = ip
        args.port = int(port)
        args.remote_mode = True
        ctx.vlog("qemu-command --> Open remote mode because the target is specified.")

    if args.ip and args.port and not args.debug_mode:
        args.remote_mode = True
        ctx.vlog("qemu-command --> Open remote mode because the ip and port are all specified.")
    
    if args.debug_mode:
        if args.remote_mode:
            cxt.abort("qemu-command --> Cannot open both debug mode and remote mode.")
    
    if args.remote_mode and not args.filename:
        ctx.vlog2("qemu-command --> You need to set context manually otherwise some bugs would occur when you use flat or packing.")
    
    if not args.remote_mode and not args.filename:
        ctx.abort("qemu-command --> Please set filename.")
    
    if args.filename:
        args.filename = ctx.gift['filename']
        context.binary = args.filename
        ctx.gift['elf'] = ELF(args.filename, checksec=False)

    if args.remote_mode:
        __remote_mode(ctx, args)
        ctx.gift['remote'] = True
    else:
        __debug_mode(ctx, args)
        ctx.gift['debug'] = True
        
    # from cli, keep interactive
    if ctx.fromcli: 
        ctx.gift['io'].interactive()
    
    

@click.command(name='qemu', short_help="Use qemu to debug pwn, for kernel pwn or arm/mips arch.")
@click.argument('filename', type=str, default=None, required=False, nargs=1)
@click.argument('target', type=str, default=None, required=False, nargs=1)
@click.option('-d', '--debug', "--debug-mode", "debug_mode", is_flag=True, help="Use debug mode or not, default is opened.")
@click.option('-r', '--remote', "--remote-mode", "remote_mode", is_flag=True, show_default=True, help="Use remote mode or not, default is debug mode.")
@click.option('-i', '--ip', default=None, show_default=True, type=str, nargs=1, help='The remote ip addr or gdb listen ip when debug.')
@click.option('-p', '--port', default=None, show_default=True, type=int, nargs=1, help='The remote port or gdb listen port when debug.')
@click.option('-L', '--lib', "lib", default=None, type=str, show_default=True, help="The lib path for current file.")
@click.option('-S', '--static', "static", is_flag=True, show_default=True, help="Use tmux to gdb-debug or not.")
@click.option('-l', '-ls', '--launch-script', "launch_script", default=None, type=str, show_default=True, help="The script for lauching the qemu, used for qemu-system mode and the command is long.")
@click.option('-t', '--use-tmux', '--tmux', "tmux", is_flag=True, show_default=True, help="Use tmux to gdb-debug or not.")
@click.option('-w', '--use-wsl', '--wsl', "wsl", is_flag=True, show_default=True, help="Use wsl to pop up windows for gdb-debug or not.")
@click.option('-g', '--use-gnome', '--gnome', "gnome", is_flag=True, show_default=True, help="Use gnome terminal to pop up windows for gdb-debug or not.")
@click.option('-G', '-gt','--gdb-type', "gdb_type", type=click.Choice(['auto', 'pwndbg', 'gef', 'peda']), nargs=1, default='auto', help="Select a gdb plugin.")
@click.option('-b', '-gb', '--gdb-breakpoint', "gdb_breakpoint", default=[], type=str, multiple=True, show_default=True, help="Set gdb breakpoints while gdb-debug is used, it should be a hex address or a function name. Multiple breakpoints are supported.")
@click.option('-s', '-gs', '--gdb-script', "gdb_script", default=None, type=str, show_default=True, help="Set gdb commands like '-ex' or '-x' while gdb-debug is used, the content will be passed to gdb and use ';' to split lines. Besides eval-commands, file path is supported.")
@click.option('-n', '-nl', '--no-log', "no_log", is_flag=True, show_default=True, help="Disable context.log or not.")
@click.option('-P', '-ns', '--no-stop', "no_stop", is_flag=True, show_default=True, help="Use the 'stop' function or not. Only for python script mode.")
@click.option('-v', '--verbose', count=True, show_default=True, help="Show more info or not.")
@pass_environ
def cli(ctx, filename, target, debug_mode, remote_mode, ip, port, lib, static, launch_script, tmux, wsl, gnome, gdb_type, gdb_breakpoint, gdb_script, no_log, no_stop, verbose):
    ctx.vlog("Welcome to use pwncli-qemu command~")
    if not ctx.verbose:
        ctx.verbose = verbose
    if verbose:
        ctx.vlog("qemu-command --> Open 'verbose' mode")

    ctx.gift['no_stop'] = no_stop

    ll = 'error' if no_log else ctx.gift['context_log_level']
    context.update(log_level=ll)
    ctx.vlog("qemu-command --> Set 'context.log_level': {}".format(ll))
    args = _Inner_Dict()
    args.filename = filename
    args.target = target
    args.debug_mode = debug_mode
    args.remote_mode = remote_mode
    args.ip = ip
    args.port = port
    args.lib = lib
    args.static = static
    args.launch_script = launch_script
    args.tmux = tmux
    args.wsl = wsl
    args.gnome = gnome
    args.gdb_type = gdb_type
    args.gdb_breakpoint = gdb_breakpoint
    args.gdb_script = gdb_script

    if args['target'] and os.path.exists(args['target']):
        tmp = args['target']
        args['target'] = args['filename']
        args['filename'] = tmp

    for k, v in args.items():
        ctx.vlog("qemu-command --> Get '{}': {}".format(k, v))
    
    __process_args(ctx, args)
