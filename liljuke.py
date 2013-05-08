#!/usr/bin/python
import collections
import json
import os
import subprocess
import sys
import threading
import time

import pygame

from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.id3 import ID3
from mutagen.easyid3 import EasyID3

try:
    import wiringpi
    wiringpi # stfu pyflakes
except ImportError:
    wiringpi = None

POLL_GPIO = 25

BACKGROUND = (0, 0, 0)
GREEN = (0, 255, 0)
YELLOW = (255, 255, 0)
TEXT = (255, 255, 255)

PAST_PENALTY = 0.95
POLL_INTERVAL = 2 # second
RESCAN_INTERVAL = 300 # 5 minutes

SCREEN_SIZE = (656, 416)
IDLE_TIMEOUT = 30

class LilJuke(object):
    ASLEEP = -1
    IDLE = 0
    PLAYING = 1
    PAUSED = 2

    def __init__(self, folder):
        print "Initializing..."
        self.folder = folder
        self.dbfile = dbfile = os.path.join(folder, '.liljuke.d', 'liljuke.db')
        if os.path.exists(dbfile):
            self.albums = [Album.from_json(album) for album in
                           json.load(open(dbfile, 'rb'))]
        else:
            self.albums = []

        self.scan_albums(folder, self.albums)
        self.save()

        self.state = self.IDLE
        self.idle_since = time.time()
        self.chill_until = 0
        self.shell_queue = collections.deque()
        self.shell_condition = threading.Condition()

    def save(self):
        data = [album.as_json() for album in self.albums]
        mkfolder(self.dbfile)
        with open(self.dbfile, 'wb') as f:
            json.dump(data, f, indent=4)

    def scan_albums(self, folder, albums):
        visited = set([album.path for album in albums])
        def visit(path, include):
            if path in visited:
                return
            files = os.listdir(path)
            include = include or '.liljuke' in files
            album = None
            for fname in files:
                ext = os.path.splitext(fname.lower())[1]
                if ext in ('.flac', '.ogg', '.mp3'):
                    if include:
                        album = Album()
                    break

            if album:
                cover = None
                for fname in files:
                    fpath = os.path.join(path, fname)
                    ext = os.path.splitext(fname.lower())[1]

                    if ext in MUSIC_EXTS:
                        track_data = get_track_data(fpath, fname, ext)
                        if not track_data:
                            return
                        discnum, tracknum = track_data
                        album.tracks.append(Track(fpath, discnum, tracknum))

                    elif not cover and ext in COVER_EXTS:
                        cover = fpath

                album.tracks.sort(key=Track.sort_key)

                if not cover:
                    cover = extract_cover(album.tracks[0].path)

                if not cover:
                    print "Skipping %s, no album cover found" % path
                    return

                scaled = os.path.join(
                    folder, '.liljuke.d', 'art', relativeto(cover, folder))
                if not os.path.exists(scaled):
                    mkfolder(scaled)
                    subprocess.check_call(['cp', cover, scaled])
                    subprocess.check_call(
                        ['mogrify', '-geometry', '%dx%d' % SCREEN_SIZE, scaled])
                album.added = os.path.getmtime(album.tracks[0].path)
                album.plays = 1
                album.path = path
                album.cover = scaled
                albums.append(album)
                print 'Added', album.path
                self.save()

            else:
                for fname in files:
                    child = os.path.join(path, fname)
                    if os.path.isdir(child):
                        visit(child, include)

        visit(folder, False)
        albums.sort(key=Album.sort_key, reverse=True)
        self.last_scan = time.time()

    def run(self, fullscreen):
        print "Running..."
        pygame.init()
        subprocess.call(['mocp', '--server'])
        if fullscreen:
            self.screen = pygame.display.set_mode((0,0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode(SCREEN_SIZE)
        pygame.mouse.set_visible(False)
        self.set_album(0)
        poll_thread = threading.Thread(target=self.poll)
        poll_thread.daemon = True
        poll_thread.start()
        shell_thread = threading.Thread(target=self.shell)
        shell_thread.daemon = True
        shell_thread.start()
        if wiringpi is not None:
            init_gpio()
            io = wiringpi.GPIO(wiringpi.GPIO.WPI_MODE_SYS)
            pygame.time.set_timer(POLL_GPIO, 5)
            knob = Knob(io)
            button = Button(io)
            self.tv = TV(io)
            self.amp = Amp(io)
        while True:
            event = pygame.event.wait()
            if event.type == pygame.KEYDOWN:
                if event.unicode == u'q':
                    sys.exit(0)
                elif event.key == 275:
                    self.jog(1)
                elif event.key == 276:
                    self.jog(-1)
                elif event.unicode == u' ':
                    self.button()

            elif event.type == POLL_GPIO:
                if self.state == self.ASLEEP:
                    if knob.read() or button.pressed():
                        self.wake_up()
                else:
                    jog = knob.read()
                    if jog:
                        self.jog(jog)
                    elif button.pressed():
                        self.button()
                    elif self.state == self.IDLE:
                        if time.time() - self.idle_since > IDLE_TIMEOUT:
                            self.fall_asleep()

    def shell(self):
        sc = self.shell_condition
        sq = self.shell_queue
        while True:
            sc.acquire()
            while not sq:
                sc.wait()
            args = sq.popleft()
            sc.release()
            subprocess.call(args)

    def poll(self):
        while True:
            time.sleep(POLL_INTERVAL)
            now = time.time()
            if now < self.chill_until:
                continue
            if self.state == self.PLAYING:
                try:
                    mocp_state = subprocess.check_output(['mocp', '--info'])
                except:
                    continue
                if now < self.chill_until:
                    continue
                if 'PLAY' in mocp_state:
                    path = mocp_state.split('\n')[1]
                    assert path.startswith('File: ')
                    path = path[6:]
                    album = self.albums[self.album]
                    for track in album.tracks:
                        if track.path == path:
                            if self.tracknum != track.tracknum:
                                self.tracknum = track.tracknum
                                self.draw()
                else:
                    # Album finished
                    self.finish_play()

            else:
                if now - self.last_scan > RESCAN_INTERVAL:
                    self.scan_albums(self.folder, self.albums)
                    self.save()

    def do(self, it):
        sc = self.shell_condition
        sc.acquire()
        self.shell_queue.append(it)
        sc.notify()
        sc.release()

    def set_album(self, i):
        self.screen.fill(BACKGROUND)
        pygame.display.flip()
        self.album = i
        album = self.albums[i]
        self.cover = pygame.image.load(album.cover).convert()
        self.draw()

    def jog(self, i):
        self.chill_out()
        if self.state == self.PAUSED:
            self.stop()
        if self.state == self.IDLE:
            self.set_album((self.album + i) % len(self.albums))
            self.idle_since = time.time()
        elif self.state == self.PLAYING:
            album = self.albums[self.album]
            next_track = self.tracknum + i
            if next_track == 0 or next_track > len(album.tracks):
                self.stop()
            else:
                self.tracknum = next_track
                self.draw()
                direction = '--next' if i > 0 else '--previous'
                for _ in xrange(abs(i)):
                    self.do(['mocp', direction])

    def button(self):
        if self.state == self.IDLE:
            self.play()
        elif self.state == self.PLAYING:
            self.pause()
        elif self.state == self.PAUSED:
            self.unpause()

    def chill_out(self):
        # tell poll to chill out for twenty seconds
        self.chill_until = time.time() + 20

    def play(self):
        self.chill_out()
        self.tracknum = 1
        self.state = self.PLAYING
        self.draw()
        tracks = [track.path for track in self.albums[self.album].tracks]
        self.do(['mocp', '--clear'])
        self.do(['mocp', '--append'] + tracks)
        self.do(['mocp', '--play'])

    def pause(self):
        self.chill_out()
        self.state = self.PAUSED
        self.draw()
        self.do(['mocp', '--pause'])

    def unpause(self):
        self.chill_out()
        self.state = self.PLAYING
        self.draw()
        self.do(['mocp', '--unpause'])

    def stop(self):
        self.chill_out()
        self.state = self.IDLE
        self.idle_since = time.time()
        self.draw()
        self.do(['mocp', '--stop'])

    def finish_play(self):
        self.state = self.IDLE
        self.idle_since = time.time()
        self.draw()
        albums = self.albums
        # Plays in the past don't count as much as recent plays
        for album in albums:
            album.plays *= PAST_PENALTY
        album = self.albums[self.album]
        album.plays += 1
        albums.sort(key=Album.sort_key, reverse=True)
        self.save()
        self.album = albums.index(album)

    def fall_asleep(self):
        self.state = self.ASLEEP
        self.tv.state = False
        self.amp.state = False

    def wake_up(self):
        self.state = self.IDLE
        self.idle_since = time.time()
        self.tv.state = True
        self.amp.state = True

    def draw(self):
        screen = self.screen
        screen.fill(BACKGROUND)
        screen.blit(self.cover, self.cover.get_rect())

        if self.state in (self.PLAYING, self.PAUSED):
            # Draw green triangle for "PLAY" state
            width, height = screen.get_size()
            top = height / 10
            left = width - width / 4
            l = width / 6
            points = [(left, top), (left, top + l),
                      (left + int(0.86 * l), top + l/2)]
            color = GREEN if self.state == self.PLAYING else YELLOW
            pygame.draw.polygon(screen, color, points)

            if self.tracknum:
                font = pygame.font.SysFont('Arial', l, True)
                tile = font.render(str(self.tracknum), True, TEXT)
                rect = tile.get_rect()
                rect.bottom = height - height / 20
                rect.left = left
                screen.blit(tile, rect)

        pygame.display.flip()


class Album(object):
    properties = ('added', 'plays', 'path', 'cover')

    def __init__(self):
        self.tracks = []

    def as_json(self):
        data = {prop: getattr(self, prop) for prop in self.properties}
        data['tracks'] = [track.as_json() for track in self.tracks]
        return data

    @classmethod
    def from_json(cls, data):
        album = cls.__new__(cls)
        for prop in album.properties:
            setattr(album, prop, data[prop])
        album.tracks = [Track.from_json(track) for track in data['tracks']]
        return album

    def is_recent(self):
        thirty_days_ago = time.time() - 30 * 24 * 3600
        return self.added > thirty_days_ago

    def sort_key(self):
        return self.is_recent(), self.plays, self.added


class Track(object):
    properties = ('discnum', 'tracknum', 'path')

    def __init__(self, path, discnum, tracknum):
        self.path = path
        self.discnum = discnum
        self.tracknum = tracknum

    def as_json(self):
        return {prop: getattr(self, prop) for prop in self.properties}

    @classmethod
    def from_json(cls, data):
        track = cls.__new__(cls)
        for prop in track.properties:
            setattr(track, prop, data[prop])
        return track

    def sort_key(self):
        return self.discnum, self.tracknum, self.path


def get_track_data(path, fname, ext):
    tags = CODECS[ext](path)
    discnum = number(tags.get('discnumber'), 1)
    tracknum = tags.get('tracknumber')
    if not tracknum:
        i = 0
        while fname[i].isdigit():
            i += 1
        if not i:
            print "Unable to find track number for %s", path
            return None
        tracknum = fname[:i]
    tracknum = number(tracknum)
    return discnum, tracknum


def extract_cover(path):
    ext = os.path.splitext(path.lower())[1]
    cover = None
    if ext == '.mp3':
        tags = ID3(path)
        cover = tags.get('APIC:')
    else:
        tags = CODECS[ext](path)
        if hasattr(tags, 'pictures') and tags.pictures:
            cover = tags.pictures[0]
    if cover:
        assert cover.mime in IMAGE_TYPES, (cover.mime, path)
        if hasattr(cover, 'encoding'):
            assert cover.encoding == 0, path
        folder = os.path.dirname(path)
        cover_path = os.path.join(folder, 'cover' + IMAGE_TYPES[cover.mime])
        with open(cover_path, 'wb') as f:
            f.write(cover.data)
        return cover_path


class Knob(object):
    pins = (22, 23, 24, 25)
    max = 2**len(pins)

    def __init__(self, io):
        self.io = io
        self.state = self._read_state()

    def _read_state(self):
        state = 0
        io = self.io
        for pin in self.pins:
            state = (state<<1) | (io.digitalRead(pin)^1)
        return state

    def read(self):
        """
        Negative is left, positive is right, 0 is no change.
        """
        prev = self.state
        state = self._read_state()
        if state == prev:
            return 0

        print state
        self.state = state
        max = self.max

        right = (state - prev) % max
        left = (prev - state) % max
        if left < right:
            return -left
        return right


class Button(object):
    pin = 4
    debounce = 5 # milliseconds

    def __init__(self, io):
        self.io = io
        self.last_press = 0
        self.state = self.io.digitalRead(self.pin)

    def pressed(self):
        prev = self.state
        if prev and pygame.time.get_ticks() - self.last_press <= self.debounce:
            return False
        self.state = state = self.io.digitalRead(self.pin)
        if not prev and state:
            self.last_press = pygame.time.get_ticks()
            return True
        return False


class Switch(object):

    def __init__(self, io):
        self.io = io
        self.io.digitalWrite(self.pin, self.initial_state)
        self.state = self.initial_state

    @apply
    def state():
        def set(self, state):
            self._state = state
            self.io.digitalWrite(self.pin, not state)  # inverter buffer
        def get(self):
            return self._state
        return property(get, set)


class TV(Switch):
    initial_state = True
    pin = 27
    

class Amp(Switch):
    initial_state = True
    pin = 18
    

def init_gpio():
    for pin in Knob.pins:
        subprocess.check_call(['gpio', 'export', str(pin), 'in'])
    subprocess.check_call(['gpio', 'export', str(Button.pin), 'in'])
    subprocess.check_call(['gpio', 'export', str(TV.pin), 'out'])
    subprocess.check_call(['gpio', 'export', str(Amp.pin), 'out'])


_marker = object()


def number(s, default=_marker):
    """
    Yes, mutagen is this annoying.
    """
    if not s and default is not _marker:
        return default
    if isinstance(s, list):
        s = s[0]
    if '/' in s:
        s, total = s.split('/')
    if not s and default is not _marker:
        return default
    return int(s)


def mkfolder(path):
    folder = os.path.dirname(path)
    if not os.path.exists(folder):
        os.makedirs(folder)


def relativeto(path, to):
    path = os.path.abspath(path)
    assert path.startswith(to), '%s is not contained in ' % (path, to)
    return path[len(to):].lstrip('/')


MUSIC_EXTS = ('.flac', '.ogg', '.mp3')
CODECS = {
    '.flac': FLAC,
    '.mp3': EasyID3,
    '.ogg': OggVorbis
}
COVER_EXTS = ('.gif', '.png', '.jpg', '.jpeg', '.bmp')
IMAGE_TYPES = {
    'image/gif': '.gif',
    'image/png': '.png',
    'image/jpg': '.jpg',
    'image/jpeg': '.jpg'}


if __name__ == '__main__':
    folder = sys.argv[1]
    assert os.path.isdir(folder)
    LilJuke(os.path.abspath(folder)).run(len(sys.argv) > 2)
