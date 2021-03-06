#!/usr/bin/env python

from __future__ import print_function
from collections import OrderedDict
from glob import glob
import os
import pipes
import platform
import re
import shutil
import subprocess
import sys
import tempfile

INCLUDE_PATTERN = re.compile("#include\s+[<\"](.*?)[>\"]")

DEVKITS = {
    "frida-gum": ("frida-gum-1.0", ("frida-1.0", "gum", "gum.h")),
    "frida-gumjs": ("frida-gumjs-1.0", ("frida-1.0", "gumjs", "gumscriptbackend.h")),
    "frida-core": ("frida-core-1.0", ("frida-1.0", "frida-core.h")),
}

def generate_devkit(kit, host, output_dir):
    package, umbrella_header = DEVKITS[kit]

    frida_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

    umbrella_header_path = compute_umbrella_header_path(frida_root, host, package, umbrella_header)

    header_filename = kit + ".h"
    if not os.path.exists(umbrella_header_path):
        raise Exception("Header not found: {}".format(umbrella_header_path))
    header = generate_header(package, frida_root, host, umbrella_header_path)
    with open(os.path.join(output_dir, header_filename), "w") as f:
        f.write(header)

    library_filename = compute_library_filename(kit)
    (library, extra_ldflags) = generate_library(package, frida_root, host)
    with open(os.path.join(output_dir, library_filename), "wb") as f:
        f.write(library)

    example_filename = kit + "-example.c"
    example = generate_example(example_filename, package, frida_root, host, kit, extra_ldflags)
    with open(os.path.join(output_dir, example_filename), "w") as f:
        f.write(example)

    return [header_filename, library_filename, example_filename]

def generate_header(package, frida_root, host, umbrella_header_path):
    if platform.system() == 'Windows':
        include_dirs = [
            r"C:\Program Files (x86)\Microsoft Visual Studio 14.0\VC\INCLUDE",
            r"C:\Program Files (x86)\Windows Kits\10\Include\10.0.14393.0\ucrt",
            os.path.join(frida_root, "build", "sdk-windows", msvs_arch_config(host), "lib", "glib-2.0", "include"),
            os.path.join(frida_root, "build", "sdk-windows", msvs_arch_config(host), "include", "glib-2.0"),
            os.path.join(frida_root, "build", "sdk-windows", msvs_arch_config(host), "include", "glib-2.0"),
            os.path.join(frida_root, "build", "sdk-windows", msvs_arch_config(host), "include", "json-glib-1.0"),
            os.path.join(frida_root, "frida-gum"),
            os.path.join(frida_root, "frida-gum", "bindings")
        ]
        includes = ["/I" + include_dir for include_dir in include_dirs]

        preprocessor = subprocess.Popen(
            [msvs_cl_exe(host), "/nologo", "/E", umbrella_header_path] + includes,
            cwd=msvs_runtime_path(host),
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        stdout, stderr = preprocessor.communicate()
        if preprocessor.returncode != 0:
            raise Exception("Failed to spawn preprocessor: " + stderr.decode('utf-8'))
        lines = stdout.decode('utf-8').split('\n')

        mapping_prefix = "#line "
        header_refs = [line[line.index("\"") + 1:line.rindex("\"")].replace("\\\\", "/") for line in lines if line.startswith(mapping_prefix)]

        # c:/ => C:/
        header_refs = [ref[0].upper() + ref[1:] for ref in header_refs]

        header_files = []
        headers_seen = set()
        for ref in header_refs:
            if ref in headers_seen:
                continue
            header_files.append(ref)
            headers_seen.add(ref)

        frida_root_slashed = frida_root.replace("\\", "/")
        header_files = [header_file for header_file in header_files if header_file.startswith(frida_root_slashed)]
    else:
        rc = env_rc(frida_root, host)
        header_dependencies = subprocess.check_output(
            ["(. \"{rc}\" && $CPP $CFLAGS -M $($PKG_CONFIG --cflags {package}) \"{header}\")".format(rc=rc, package=package, header=umbrella_header_path)],
            shell=True).decode('utf-8')
        header_lines = header_dependencies.strip().split("\n")[1:]
        header_files = [line.rstrip("\\").strip() for line in header_lines]
        header_files = [header_file for header_file in header_files if header_file.startswith(frida_root)]

    devkit_header_lines = []
    umbrella_header = header_files[0]
    processed_header_files = set([umbrella_header])
    ingest_header(umbrella_header, header_files, processed_header_files, devkit_header_lines)
    devkit_header = "".join(devkit_header_lines)

    if package.startswith("gum"):
        config = "#define GUM_STATIC\n\n"
    else:
        config = ""

    return config + devkit_header

def ingest_header(header, all_header_files, processed_header_files, result):
    with open(header, "r") as f:
        for line in f:
            match = INCLUDE_PATTERN.match(line.strip())
            if match is not None:
                name = match.group(1)
                inline = False
                for other_header in all_header_files:
                    if other_header.endswith("/" + name):
                        inline = True
                        if not other_header in processed_header_files:
                            processed_header_files.add(other_header)
                            ingest_header(other_header, all_header_files, processed_header_files, result)
                        break
                if not inline:
                    result.append(line)
            else:
                result.append(line)

def generate_library(package, frida_root, host):
    if platform.system() == 'Windows':
        return generate_library_windows(package, frida_root, host)
    else:
        return generate_library_unix(package, frida_root, host)

def generate_library_windows(package, frida_root, host):
    glib = [
        sdk_lib_path("glib-2.0.lib", frida_root, host),
        sdk_lib_path("intl.lib", frida_root, host),
    ]
    gobject = glib + [
        sdk_lib_path("gobject-2.0.lib", frida_root, host),
        sdk_lib_path("ffi.lib", frida_root, host),
    ]
    gmodule = glib + [
        sdk_lib_path("gmodule-2.0.lib", frida_root, host),
    ]
    gio = glib + gobject + gmodule + [
        sdk_lib_path("gio-2.0.lib", frida_root, host),
        sdk_lib_path("z.lib", frida_root, host),
    ]

    json_glib = glib + gobject + [
        sdk_lib_path("json-glib-1.0.lib", frida_root, host),
    ]

    gee = glib + gobject + [
        sdk_lib_path("gee-0.8.lib", frida_root, host),
    ]

    v8 = [
        sdk_lib_path("v8_base_0.lib", frida_root, host),
        sdk_lib_path("v8_base_1.lib", frida_root, host),
        sdk_lib_path("v8_base_2.lib", frida_root, host),
        sdk_lib_path("v8_base_3.lib", frida_root, host),
        sdk_lib_path("v8_libbase.lib", frida_root, host),
        sdk_lib_path("v8_libplatform.lib", frida_root, host),
        sdk_lib_path("v8_libsampler.lib", frida_root, host),
        sdk_lib_path("v8_snapshot.lib", frida_root, host),
    ]

    gum_deps = deduplicate(glib + gobject + gio)
    gumjs_deps = deduplicate(gum_deps + json_glib + v8)
    frida_core_deps = deduplicate(glib + gobject + gio + json_glib + gmodule + gee)

    if package == "frida-gum-1.0":
        package_lib_path = internal_arch_lib_path("gum", frida_root, host)
        package_lib_deps = gum_deps
    elif package == "frida-gumjs-1.0":
        package_lib_path = internal_arch_lib_path("gumjs", frida_root, host)
        package_lib_deps = gumjs_deps
    elif package == "frida-core-1.0":
        package_lib_path = internal_noarch_lib_path("frida-core", frida_root, host)
        package_lib_deps = frida_core_deps
    else:
        raise Exception("Unhandled package")

    combined_dir = tempfile.mkdtemp(prefix="devkit")
    object_names = set()

    for lib_path in [package_lib_path] + package_lib_deps:
        archive_filenames = subprocess.check_output(
            [msvs_lib_exe(host), "/nologo", "/list", lib_path],
            cwd=msvs_runtime_path(host),
            shell=False).decode('utf-8').replace("\r", "").rstrip().split("\n")

        original_object_names = [name for name in archive_filenames if name.endswith(".obj")]

        for original_object_name in original_object_names:
            object_name = os.path.basename(original_object_name)
            while object_name in object_names:
                object_name = "_" + object_name
            object_names.add(object_name)

            object_path = os.path.join(combined_dir, object_name)

            subprocess.check_output(
                [msvs_lib_exe(host), "/nologo", "/extract:" + original_object_name, "/out:" + object_path, lib_path],
                cwd=msvs_runtime_path(host),
                shell=False).decode('utf-8')

    library_path = os.path.join(combined_dir, "library.lib")

    env = os.environ.copy()
    env["PATH"] = msvs_runtime_path(host) + ";" + env["PATH"]

    subprocess.check_output(
        [msvs_lib_exe(host), "/nologo", "/out:library.lib", "*.obj"],
        cwd=combined_dir,
        env=env,
        shell=False)

    with open(library_path, "rb") as f:
        data = f.read()
    extra_flags = []

    shutil.rmtree(combined_dir)

    return (data, extra_flags)

def generate_library_unix(package, frida_root, host):
    rc = env_rc(frida_root, host)

    library_flags = subprocess.check_output(
        ["(. \"{rc}\" && $PKG_CONFIG --static --libs {package})".format(rc=rc, package=package)],
        shell=True).decode('utf-8').strip().split(" ")
    library_dirs = infer_library_dirs(library_flags)
    library_names = infer_library_names(library_flags)
    library_paths, extra_flags = resolve_library_paths(library_names, library_dirs)
    extra_flags += infer_linker_flags(library_flags)

    combined_dir = tempfile.mkdtemp(prefix="devkit")
    object_names = set()

    for library_path in library_paths:
        scratch_dir = tempfile.mkdtemp(prefix="devkit")

        subprocess.check_output(
            ["(. \"{rc}\" && $AR x {library_path})".format(rc=rc, library_path=library_path)],
            shell=True,
            cwd=scratch_dir)
        for object_path in glob(os.path.join(scratch_dir, "*.o")):
            object_name = os.path.basename(object_path)
            while object_name in object_names:
                object_name = "_" + object_name
            object_names.add(object_name)
            shutil.move(object_path, os.path.join(combined_dir, object_name))

        shutil.rmtree(scratch_dir)

    library_path = os.path.join(combined_dir, "library.a")
    subprocess.check_output(
        ["(. \"{rc}\" && $AR rcs {library_path} {object_files} 2>/dev/null)".format(
            rc=rc,
            library_path=library_path,
            object_files=" ".join([pipes.quote(object_name) for object_name in object_names]))],
        shell=True,
        cwd=combined_dir)
    with open(library_path, "rb") as f:
        data = f.read()

    shutil.rmtree(combined_dir)

    return (data, extra_flags)

def infer_library_dirs(flags):
    return [flag[2:] for flag in flags if flag.startswith("-L")]

def infer_library_names(flags):
    return [flag[2:] for flag in flags if flag.startswith("-l")]

def infer_linker_flags(flags):
    return [flag for flag in flags if flag.startswith("-Wl")]

def resolve_library_paths(names, dirs):
    paths = []
    flags = []
    for name in names:
        library_path = None
        for d in dirs:
            candidate = os.path.join(d, "lib{}.a".format(name))
            if os.path.exists(candidate):
                library_path = candidate
                break
        if library_path is not None:
            paths.append(library_path)
        else:
            flags.append("-l{}".format(name))
    return (list(set(paths)), flags)

def generate_example(filename, package, frida_root, host, library_name, extra_ldflags):
    if platform.system() != 'Windows':
        rc = env_rc(frida_root, host)

        cc = probe_env(rc, "echo $CC")
        cflags = probe_env(rc, "echo $CFLAGS")
        ldflags = probe_env(rc, "echo $LDFLAGS")

        (cflags, ldflags) = trim_flags(cflags, " ".join([" ".join(extra_ldflags), ldflags]))

        params = {
            "cc": cc,
            "cflags": cflags,
            "ldflags": ldflags,
            "source_filename": filename,
            "program_filename": os.path.splitext(filename)[0],
            "library_name": library_name
        }

        preamble = """\
/*
 * Compile with:
 *
 * %(cc)s %(cflags)s %(source_filename)s -o %(program_filename)s -L. -l%(library_name)s %(ldflags)s
 *
 * See www.frida.re for documentation.
 */""" % params
    else:
        preamble = """\
/*
 * Link with:
 *
 * frida-gum.lib;dnsapi.lib;iphlpapi.lib;psapi.lib;winmm.lib;ws2_32.lib
 *
 * See www.frida.re for documentation.
 */"""

    if package == "frida-gum-1.0":
        return r"""%(preamble)s

#include "frida-gum.h"

#include <fcntl.h>
#include <unistd.h>

typedef struct _ExampleListener ExampleListener;
typedef enum _ExampleHookId ExampleHookId;

struct _ExampleListener
{
  GObject parent;

  guint num_calls;
};

enum _ExampleHookId
{
  EXAMPLE_HOOK_OPEN,
  EXAMPLE_HOOK_CLOSE
};

static void example_listener_iface_init (gpointer g_iface, gpointer iface_data);

#define EXAMPLE_TYPE_LISTENER (example_listener_get_type ())
G_DECLARE_FINAL_TYPE (ExampleListener, example_listener, EXAMPLE, LISTENER, GObject)
G_DEFINE_TYPE_EXTENDED (ExampleListener,
                        example_listener,
                        G_TYPE_OBJECT,
                        0,
                        G_IMPLEMENT_INTERFACE (GUM_TYPE_INVOCATION_LISTENER,
                            example_listener_iface_init))

int
main (int argc,
      char * argv[])
{
  GumInterceptor * interceptor;
  GumInvocationListener * listener;

  gum_init ();

  interceptor = gum_interceptor_obtain ();
  listener = g_object_new (EXAMPLE_TYPE_LISTENER, NULL);

  gum_interceptor_begin_transaction (interceptor);
  gum_interceptor_attach_listener (interceptor,
      GSIZE_TO_POINTER (gum_module_find_export_by_name (NULL, "open")),
      listener,
      GSIZE_TO_POINTER (EXAMPLE_HOOK_OPEN));
  gum_interceptor_attach_listener (interceptor,
      GSIZE_TO_POINTER (gum_module_find_export_by_name (NULL, "close")),
      listener,
      GSIZE_TO_POINTER (EXAMPLE_HOOK_CLOSE));
  gum_interceptor_end_transaction (interceptor);

  close (open ("/etc/hosts", O_RDONLY));
  close (open ("/etc/fstab", O_RDONLY));

  g_print ("[*] listener got %%u calls\n", EXAMPLE_LISTENER (listener)->num_calls);

  gum_interceptor_detach_listener (interceptor, listener);

  close (open ("/etc/hosts", O_RDONLY));
  close (open ("/etc/fstab", O_RDONLY));

  g_print ("[*] listener still has %%u calls\n", EXAMPLE_LISTENER (listener)->num_calls);

  g_object_unref (listener);
  g_object_unref (interceptor);

  return 0;
}

static void
example_listener_on_enter (GumInvocationListener * listener,
                           GumInvocationContext * ic)
{
  ExampleListener * self = EXAMPLE_LISTENER (listener);
  ExampleHookId hook_id = GUM_LINCTX_GET_FUNC_DATA (ic, ExampleHookId);

  switch (hook_id)
  {
    case EXAMPLE_HOOK_OPEN:
      g_print ("[*] open(\"%%s\")\n", gum_invocation_context_get_nth_argument (ic, 0));
      break;
    case EXAMPLE_HOOK_CLOSE:
      g_print ("[*] close(%%d)\n", (int) gum_invocation_context_get_nth_argument (ic, 0));
      break;
  }

  self->num_calls++;
}

static void
example_listener_on_leave (GumInvocationListener * listener,
                           GumInvocationContext * ic)
{
}

static void
example_listener_class_init (ExampleListenerClass * klass)
{
  (void) EXAMPLE_IS_LISTENER;
  (void) glib_autoptr_cleanup_ExampleListener;
}

static void
example_listener_iface_init (gpointer g_iface,
                             gpointer iface_data)
{
  GumInvocationListenerIface * iface = (GumInvocationListenerIface *) g_iface;

  iface->on_enter = example_listener_on_enter;
  iface->on_leave = example_listener_on_leave;
}

static void
example_listener_init (ExampleListener * self)
{
}
""" % { "preamble": preamble }
    elif package == "frida-gumjs-1.0":
        return r"""%(preamble)s

#include "frida-gumjs.h"

#include <fcntl.h>
#include <string.h>
#include <unistd.h>

static void on_message (GumScript * script, const gchar * message, GBytes * data, gpointer user_data);

int
main (int argc,
      char * argv[])
{
  GumScriptBackend * backend;
  GCancellable * cancellable = NULL;
  GError * error = NULL;
  GumScript * script;
  GMainContext * context;

  gum_init ();

  backend = gum_script_backend_obtain_duk ();

  script = gum_script_backend_create_sync (backend, "example",
      "Interceptor.attach(Module.findExportByName(null, \"open\"), {\n"
      "  onEnter: function (args) {\n"
      "    console.log(\"[*] open(\\\"\" + Memory.readUtf8String(args[0]) + \"\\\")\");\n"
      "  }\n"
      "});\n"
      "Interceptor.attach(Module.findExportByName(null, \"close\"), {\n"
      "  onEnter: function (args) {\n"
      "    console.log(\"[*] close(\" + args[0].toInt32() + \")\");\n"
      "  }\n"
      "});",
      cancellable, &error);
  g_assert (error == NULL);

  gum_script_set_message_handler (script, on_message, NULL, NULL);

  gum_script_load_sync (script, cancellable);

  close (open ("/etc/hosts", O_RDONLY));
  close (open ("/etc/fstab", O_RDONLY));

  context = g_main_context_get_thread_default ();
  while (g_main_context_pending (context))
    g_main_context_iteration (context, FALSE);

  gum_script_unload_sync (script, cancellable);

  g_object_unref (script);

  return 0;
}

static void
on_message (GumScript * script,
            const gchar * message,
            GBytes * data,
            gpointer user_data)
{
  JsonParser * parser;
  JsonObject * root;
  const gchar * type;

  parser = json_parser_new ();
  json_parser_load_from_data (parser, message, -1, NULL);
  root = json_node_get_object (json_parser_get_root (parser));

  type = json_object_get_string_member (root, "type");
  if (strcmp (type, "log") == 0)
  {
    const gchar * log_message;

    log_message = json_object_get_string_member (root, "payload");
    g_print ("%%s\n", log_message);
  }
  else
  {
    g_print ("on_message: %%s\n", message);
  }

  g_object_unref (parser);
}
""" % { "preamble": preamble }
    elif package == "frida-core-1.0":
        return r"""%(preamble)s

#include "frida-core.h"

#include <stdlib.h>
#include <string.h>

static void on_message (FridaScript * script, const gchar * message, GBytes * data, gpointer user_data);
static void on_signal (int signo);
static gboolean stop (gpointer user_data);

static GMainLoop * loop = NULL;

int
main (int argc,
      char * argv[])
{
  guint target_pid;
  FridaDeviceManager * manager;
  GError * error = NULL;
  FridaDeviceList * devices;
  gint num_devices, i;
  FridaDevice * local_device;
  FridaSession * session;

  if (argc != 2 || (target_pid = atoi (argv[1])) == 0)
  {
    g_printerr ("Usage: %%s <pid>\n", argv[0]);
    return 1;
  }

  frida_init ();

  loop = g_main_loop_new (NULL, TRUE);

  signal (SIGINT, on_signal);
  signal (SIGTERM, on_signal);

  manager = frida_device_manager_new ();

  devices = frida_device_manager_enumerate_devices_sync (manager, &error);
  g_assert (error == NULL);

  local_device = NULL;
  num_devices = frida_device_list_size (devices);
  for (i = 0; i != num_devices; i++)
  {
    FridaDevice * device = frida_device_list_get (devices, i);

    g_print ("[*] Found device: \"%%s\"\n", frida_device_get_name (device));

    if (frida_device_get_dtype (device) == FRIDA_DEVICE_TYPE_LOCAL)
      local_device = g_object_ref (device);

    g_object_unref (device);
  }
  g_assert (local_device != NULL);

  frida_unref (devices);
  devices = NULL;

  session = frida_device_attach_sync (local_device, target_pid, &error);
  if (error == NULL)
  {
    FridaScript * script;

    g_print ("[*] Attached\n");

    script = frida_session_create_script_sync (session, "example",
        "Interceptor.attach(Module.findExportByName(null, \"open\"), {\n"
        "  onEnter: function (args) {\n"
        "    console.log(\"[*] open(\\\"\" + Memory.readUtf8String(args[0]) + \"\\\")\");\n"
        "  }\n"
        "});\n"
        "Interceptor.attach(Module.findExportByName(null, \"close\"), {\n"
        "  onEnter: function (args) {\n"
        "    console.log(\"[*] close(\" + args[0].toInt32() + \")\");\n"
        "  }\n"
        "});",
        &error);
    g_assert (error == NULL);

    g_signal_connect (script, "message", G_CALLBACK (on_message), NULL);

    frida_script_load_sync (script, &error);
    g_assert (error == NULL);

    g_print ("[*] Script loaded\n");

    if (g_main_loop_is_running (loop))
      g_main_loop_run (loop);

    g_print ("[*] Stopped\n");

    frida_script_unload_sync (script, NULL);
    frida_unref (script);
    g_print ("[*] Unloaded\n");

    frida_session_detach_sync (session);
    frida_unref (session);
    g_print ("[*] Detached\n");
  }
  else
  {
    g_printerr ("Failed to attach: %%s\n", error->message);
    g_error_free (error);
  }

  frida_unref (local_device);

  frida_device_manager_close_sync (manager);
  frida_unref (manager);
  g_print ("[*] Closed\n");

  g_main_loop_unref (loop);

  return 0;
}

static void
on_message (FridaScript * script,
            const gchar * message,
            GBytes * data,
            gpointer user_data)
{
  JsonParser * parser;
  JsonObject * root;
  const gchar * type;

  parser = json_parser_new ();
  json_parser_load_from_data (parser, message, -1, NULL);
  root = json_node_get_object (json_parser_get_root (parser));

  type = json_object_get_string_member (root, "type");
  if (strcmp (type, "log") == 0)
  {
    const gchar * log_message;

    log_message = json_object_get_string_member (root, "payload");
    g_print ("%%s\n", log_message);
  }
  else
  {
    g_print ("on_message: %%s\n", message);
  }

  g_object_unref (parser);
}

static void
on_signal (int signo)
{
  g_idle_add (stop, NULL);
}

static gboolean
stop (gpointer user_data)
{
  g_main_loop_quit (loop);

  return FALSE;
}
""" % { "preamble": preamble }

def env_rc(frida_root, host):
    return os.path.join(frida_root, "build", "frida-env-{}.rc".format(host))

def msvs_cl_exe(host):
    return msvs_tool_path(host, "cl.exe")

def msvs_lib_exe(host):
    return msvs_tool_path(host, "lib.exe")

def msvs_tool_path(host, tool):
    if host == "windows-x86_64":
        return r"C:\Program Files (x86)\Microsoft Visual Studio 14.0\VC\bin\amd64\{0}".format(tool)
    else:
        return r"C:\Program Files (x86)\Microsoft Visual Studio 14.0\VC\bin\amd64_x86\{0}".format(tool)

def msvs_runtime_path(host):
    return r"C:\Program Files (x86)\Microsoft Visual Studio 14.0\VC\bin\amd64"

def msvs_arch_config(host):
    if host == "windows-x86_64":
        return "x64-Release"
    else:
        return "Win32-Release"

def msvs_arch_suffix(host):
    if host == "windows-x86_64":
        return "-64"
    else:
        return "-32"

def compute_library_filename(kit):
    if platform.system() == 'Windows':
        return "{}.lib".format(kit)
    else:
        return "lib{}.a".format(kit)

def compute_umbrella_header_path(frida_root, host, package, umbrella_header):
    if platform.system() == 'Windows':
        if package == "frida-gum-1.0":
            return os.path.join(frida_root, "frida-gum", "gum", "gum.h")
        elif package == "frida-gumjs-1.0":
            return os.path.join(frida_root, "frida-gum", "bindings", "gumjs", umbrella_header[-1])
        elif package == "frida-core-1.0":
            return os.path.join(frida_root, "build", "tmp-windows", msvs_arch_config(host), "frida-core", "api", "frida-core.h")
        else:
            raise Exception("Unhandled package")
    else:
        return os.path.join(frida_root, "build", "frida-" + host, "include", *umbrella_header)

def sdk_lib_path(name, frida_root, host):
    return os.path.join(frida_root, "build", "sdk-windows", msvs_arch_config(host), "lib", name)

def internal_noarch_lib_path(name, frida_root, host):
    return os.path.join(frida_root, "build", "tmp-windows", msvs_arch_config(host), name, name + ".lib")

def internal_arch_lib_path(name, frida_root, host):
    lib_name = name + msvs_arch_suffix(host)
    return os.path.join(frida_root, "build", "tmp-windows", msvs_arch_config(host), lib_name, lib_name + ".lib")

def probe_env(rc, command):
    return subprocess.check_output([
        "(. \"{rc}\" && PACKAGE_TARNAME=frida-devkit . $CONFIG_SITE && {command})".format(rc=rc, command=command)
    ], shell=True).decode('utf-8').strip()

def trim_flags(cflags, ldflags):
    trimmed_cflags = []
    trimmed_ldflags = []

    pending_cflags = cflags.split(" ")
    while len(pending_cflags) > 0:
        flag = pending_cflags.pop(0)
        if flag == "-include":
            pending_cflags.pop(0)
        else:
            trimmed_cflags.append(flag)

    trimmed_cflags = deduplicate(trimmed_cflags)
    existing_cflags = set(trimmed_cflags)

    pending_ldflags = ldflags.split(" ")
    while len(pending_ldflags) > 0:
        flag = pending_ldflags.pop(0)
        if flag in ("-arch", "-isysroot") and flag in existing_cflags:
            pending_ldflags.pop(0)
        else:
            trimmed_ldflags.append(flag)

    pending_ldflags = trimmed_ldflags
    trimmed_ldflags = []
    while len(pending_ldflags) > 0:
        flag = pending_ldflags.pop(0)

        raw_flags = []
        while flag.startswith("-Wl,"):
            raw_flags.append(flag[4:])
            if len(pending_ldflags) > 0:
                flag = pending_ldflags.pop(0)
            else:
                flag = None
                break
        if len(raw_flags) > 0:
            trimmed_ldflags.append("-Wl," + ",".join(raw_flags))

        if flag is not None and flag not in existing_cflags:
            trimmed_ldflags.append(flag)

    return (" ".join(trimmed_cflags), " ".join(trimmed_ldflags))

def deduplicate(items):
    return list(OrderedDict.fromkeys(items))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: {0} kit host outdir".format(sys.argv[0]), file=sys.stderr)
        sys.exit(1)

    kit = sys.argv[1]
    host = sys.argv[2]
    outdir = sys.argv[3]

    try:
        os.makedirs(outdir)
    except:
        pass

    generate_devkit(kit, host, outdir)
