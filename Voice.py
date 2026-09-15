# pip install pyttsx3

from threading import Thread
import kthread
import subprocess
import sys
import queue
if sys.platform == "win32":
    import pyttsx3
from time import sleep

#rate = voiceEngine.getProperty('rate')
#volume = voiceEngine.getProperty('volume')
#voice = voiceEngine.getProperty('voice')
#voiceEngine.setProperty('rate', newVoiceRate)
#voiceEngine.setProperty('voice', voice.id)   id = 0, 1, ...

"""
File:Voice.py    

Description:
  Class to enapsulate the Text to Speech package in python

To Use:
  See main() at bottom as example
  
Author: sumzer0@yahoo.com

"""


class Voice:

    def __init__(self): 
        self.q = queue.Queue(5)
        self.v_enabled = False
        self.v_quit = False
        # Initializing SAPI during application startup can hang under Wine.
        # Start the worker lazily only when voice output is enabled.
        self.t = None
        self.v_id = 1

    def say(self, vSay):
        if self.v_enabled:
            # A better way to correct mis-pronunciation?
            vSay = vSay.replace(' Mk V ', ' mark five ')
            vSay = vSay.replace(' Mk ', ' mark ')
            vSay = vSay.replace(' Krait ', ' crate ')
            self.q.put(vSay)

    def set_off(self):
        self.v_enabled = False

    def set_on(self):
        self.v_enabled = True
        if self.t is None or not self.t.is_alive():
            self.v_quit = False
            self.t = kthread.KThread(target=self.voice_exec, name="Voice", daemon=True)
            self.t.start()
        
    def set_voice_id(self, id):
        self.v_id = id

    def quit(self):
        self.v_quit = True
        
    def voice_exec(self):
        if sys.platform == "darwin":
            while not self.v_quit:
                try:
                    words = self.q.get(timeout=1)
                    self.q.task_done()
                    if words is not None:
                        subprocess.run(["/usr/bin/say", "-r", "160", str(words)], check=False)
                except queue.Empty:
                    pass
            return
        try:
            engine = pyttsx3.init()
        except Exception as exc:
            self.v_enabled = False
            print(f"Voice output is unavailable: {exc}")
            return
        voices = engine.getProperty('voices')
        v_id_current = 0   # David
        engine.setProperty('voice', voices[v_id_current].id)   
        engine.setProperty('rate', 160)
        while not self.v_quit:
            # check if the voice ID changed
            if self.v_id != v_id_current:
                v_id_current = self.v_id
                try:
                    engine.setProperty('voice', voices[v_id_current].id) 
                except:
                    print("Voice ID out of range")
                           
            try:
                words = self.q.get(timeout=1)
                self.q.task_done()
                if words is not None:
                    engine.say(words)
                    engine.runAndWait()
            except:
                pass


def main():
    v = Voice()
    v.set_on()
    sleep(2)
    v.say("Hey dude")
    sleep(2)
    v.say("whats up")
    sleep(2)
    v.quit()


if __name__ == "__main__":
    main()
