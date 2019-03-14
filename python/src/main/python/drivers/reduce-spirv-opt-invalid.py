#!/usr/bin/env python3

# Copyright 2019 The GraphicsFuzz Project Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""
This script is designed to automate the process of reducing a GLSL shader for which invalid SPIR-V
code has been found to be generated by spirv-opt when the shader is processed by the Vulkan worker.
The usage scenario is that you point the script at a shader job result, which is assumed to be in
the standard place for shader job results in a GraphicsFuzz server's working directory, and the
script will automatically work out which spirv-opt options were used, what the error from spirv-val
is, and will fetch the relevant files, kick off a reduction with glsl-reduce to find the smallest
GLSL shader and opt flags that trigger the problem, and then kick of a subsequent reduction with
spirv-reduce to try to make the reduced spir-v even smaller.
"""


import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
from typing import List


EXPECTED_STRING_RUNNING_OPTIMIZER = 'Running optimizer'
EXPECTED_STRING_INVALID_SHADER = 'Invalid shader: '


def get_validator_error_string(error_file: str) -> str:
    lines = open(error_file, 'r').readlines()  # type: List[str]
    for line in lines:
        m = re.search(EXPECTED_STRING_INVALID_SHADER + '(.*)\n', line)
        if not m:
            continue
        # This is the error string, but it may contain numbers that are context-sensitive
        error_string = m.group(1)
        # ...so we return an abstracted string with numbers replaced by '_'
        return re.sub('\d+', '_', error_string)
    raise ValueError('No line starting "'
                     + EXPECTED_STRING_INVALID_SHADER
                     + '" found in "' + error_file + '"')


def get_spirv_opt_flags(error_file: str) -> List[str]:
    lines = open(error_file, 'r').readlines()  # type: List[str]
    optimizer_command = None
    for i in range(0, len(lines) - 1):
        if lines[i].startswith(EXPECTED_STRING_RUNNING_OPTIMIZER):
            optimizer_command = lines[i + 1]
            break
    if not optimizer_command:
        raise ValueError('No line starting "'
                         + EXPECTED_STRING_RUNNING_OPTIMIZER
                         + '" found in "' + error_file + '"')
    # The expected format of the optimizer command is:
    # Exec:['/path/to/spirv-opt', '/path/to/input.spv', '-o', '/path/to/output.spv', FLAGS]\n
    # We want to extract the flags as a list of strings.
    # To do this, we split on ', ', discard the first 4 components, and then remove the quotes
    # from the remaining components.
    return list(map(lambda quoted_flag: quoted_flag[1:len(quoted_flag)-1],
                optimizer_command[0:len(optimizer_command)-2].split(', ')[4:]))


def write_interestingness_test(output_dir: str, spirvopt_flags: List[str],
                               validator_error_string: str):
    interesting_filename = output_dir + os.sep + 'interesting.py'
    with open(interesting_filename, 'w') as interesting:
        interesting.write('#!/usr/bin/python3\n\n')
        interesting.write('import os\n')
        interesting.write('import re\n')
        interesting.write('import subprocess\n')
        interesting.write('import sys\n')
        interesting.write('shaderjob_prefix = os.path.splitext(sys.argv[1])[0]\n')
        interesting.write('frag =  shaderjob_prefix + ".frag"\n')
        interesting.write('unoptimized_spv = shaderjob_prefix + ".spv"\n')
        interesting.write('optimized_spv = shaderjob_prefix + ".opt.spv"\n')
        interesting.write('cmd = [ "glslangValidator", "-V", frag, "-o", unoptimized_spv ]\n')
        interesting.write('result = subprocess.run(cmd)\n')
        interesting.write('if result.returncode != 0:\n')
        interesting.write('  print("Not interesting: glslangValidator rejected the shader.")\n')
        interesting.write('  sys.exit(1)\n')
        interesting.write('cmd = [ "spirv-opt", unoptimized_spv, "-o", optimized_spv, ' +
                          ', '.join(list(map(lambda flag: "'" + flag + "'", spirvopt_flags)))
                          + ']\n')
        interesting.write('result = subprocess.run(cmd)\n')
        interesting.write('if result.returncode != 0:\n')
        interesting.write('  print("Not interesting: spirv-opt failed on the shader.")\n')
        interesting.write('  sys.exit(2)\n')
        interesting.write('cmd = [ "spirv-val", optimized_spv ]\n')
        interesting.write('result = subprocess.run(cmd, stderr=subprocess.PIPE)\n')
        interesting.write('if result.returncode == 0:\n')
        interesting.write('  print("Not interesting: spirv-val succeeded.")\n')
        interesting.write('  sys.exit(3)\n')
        interesting.write(
            'abstracted_error_string = re.sub(\'\\d+\', \'_\', '
            'str(result.stderr, \'utf-8\').split("\\n")[0])\n')
        interesting.write('if abstracted_error_string != "' + validator_error_string + '":\n')
        interesting.write(
            '  print("Not interesting: error output \'" + abstracted_error_string + "\' did not '
            'match \'' + validator_error_string + '\'")\n')
        interesting.write('  sys.exit(4)\n')
        interesting.write('print("Interesting!")\n')
        interesting.write('sys.exit(0)\n')
    # Make the interestingness test executable
    st = os.stat(interesting_filename)
    os.chmod(interesting_filename, st.st_mode | stat.S_IEXEC)

def main_helper(args):
    description = (
        'Reduce a shader for which glslang + spirv-opt is generating code that spirv-val rejects.')

    parser = argparse.ArgumentParser(description=description)

    # Required arguments
    parser.add_argument('result_file', help='The .info.json file associated with the bad result')

    parser.add_argument(
        'output_dir',
        help='Output directory in which to place files for the reduction')

    args = parser.parse_args(args)

    result_file = os.path.abspath(args.result_file)  # type: str
    output_dir = args.output_dir

    if not os.path.isfile(result_file):
        raise FileNotFoundError('Did not find "' + result_file + '"')

    if not result_file.endswith('.info.json'):
        raise ValueError('Result file must have extension ".info.json"')

    # Make the output directory if it does not yet exist.
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir)

    # Suppose result_file is '/data/work/processing/pixel3/frag_squares/variant_185.info.json'

    # shader_job_name will be 'variant_185'
    shader_job_name = os.path.basename(os.path.splitext(os.path.splitext(result_file)[0])[0])
    # shader_family_name will be 'frag_squares'
    shader_family_name = os.path.basename(os.path.dirname(result_file))

    # error_text_file will be '/data/work/processing/pixel3/frag_squares/variant_185.txt'
    error_text_file = os.path.dirname(result_file) + os.sep + shader_job_name + '.txt'  # type: str

    if not os.path.isfile(error_text_file):
        raise FileNotFoundError('For a spirv-opt validity error there should be a file named "'
                                + error_text_file + '"')

    spirv_opt_flags = get_spirv_opt_flags(error_text_file)  # type: List[str]

    validator_error_string = get_validator_error_string(error_text_file)  # type: str

    # original_shader_without_extension will be:
    # '/data/work/shaderfamilies/frag_squares/variant_185'
    original_shader_without_extension = result_file
    for i in range(0, 4):
        original_shader_without_extension = os.path.dirname(original_shader_without_extension)
    original_shader_without_extension += (
            os.sep + 'shaderfamilies' + os.sep + shader_family_name + os.sep + shader_job_name)

    # For now we assume we are working with a .frag shader.  This will need to be extended to
    # handle other shader kinds, as well as multiple combinations of shaders, in due course.
    original_shader = original_shader_without_extension + '.frag'

    if not os.path.exists(original_shader):
        raise FileNotFoundError('Did not find original shader "' + original_shader + '"')

    shutil.copy(original_shader, output_dir + os.sep + 'shader.frag')
    shutil.copy(original_shader_without_extension + '.json',
                output_dir + os.sep + 'shader.json')

    write_interestingness_test(output_dir, spirv_opt_flags, validator_error_string)

    cmd = ['glsl-reduce', output_dir + os.sep + 'shader.json',
           output_dir + os.sep + 'interesting.py', '--output',
           output_dir ]
    subprocess.run(cmd)


if __name__ == '__main__':
    main_helper(sys.argv[1:])
