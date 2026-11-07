import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import fix_hosted_ffmpeg as F  # noqa: E402

BOT = '''import os
import subprocess
import telebot

def f():
        cmd = [
            "ffmpeg", "-y", "-i", "a.mp4",
            "-c:v", "mpeg4",
        ]
        subprocess.run(cmd)
        cmd_alt = ["ffmpeg", "-y"]
'''


def test_code_fix_replaces_commands_and_defines_ffmpeg():
    new, notes = F.plan_code_fix(BOT)
    assert new and '"ffmpeg"' not in new.split("def f")[1] and new.count("FFMPEG,") == 2
    assert new.index("FFMPEG =") < new.index("def f") and "import telebot" in new
    compile(new, "x", "exec")
    again, n2 = F.plan_code_fix(new)
    assert again is None and "از قبل" in n2[0]


def test_code_fix_none_without_commands_and_warns_on_shell():
    assert F.plan_code_fix("import os\nx=1\n")[0] is None
    new, notes = F.plan_code_fix('import subprocess\nsubprocess.run(["ffmpeg","-i","a"])\nsubprocess.run("ffmpeg -i a", shell=True)\n')
    assert new and any("shell" in n for n in notes)


def test_requirements_swap():
    new, notes = F.plan_requirements("pyTelegramBotAPI\nrequests\nffmpeg\npillow\n")
    assert new == "pyTelegramBotAPI\nrequests\npillow\nimageio-ffmpeg\n"
    assert F.plan_requirements(new)[0] is None
    assert F.plan_requirements(None)[0] is None
