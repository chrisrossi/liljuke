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


BACKGROUND = (0, 0, 0)
GREEN = (0, 255, 0)
YELLOW = (255, 255, 0)
TEXT = (255, 255, 255)

PAST_PENALTY = 0.95
POLL_INTERVAL = 2 # second
RESCAN_INTERVAL = 300 # 5 minutes


class LilJuke(object):
    IDLE = 0
    PLAYING = 1
    PAUSED = 2

    def __init__(self, folder):
        print "Initializing..."
        self.folder = folder
        self.dbfile = dbfile = os.path.join(folder, '.liljuke.db')
        if os.path.exists(dbfile):
            self.albums = [Album.from_json(album) for album in
                           json.load(open(dbfile, 'rb'))]
        else:
            self.albums = []

        self.scan_albums(folder, self.albums)
        self.save()

        self.state = self.IDLE
        self.chill_until = 0
        self.shell_queue = collections.deque()
        self.shell_condition = threading.Condition()

    def save(self):
        data = [album.as_json() for album in self.albums]
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

                album.added = os.path.getmtime(album.tracks[0].path)
                album.plays = 1
                album.path = path
                album.cover = cover
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
            self.screen = pygame.display.set_mode((656, 416))
        pygame.mouse.set_visible(False)
        self.set_album(0)
        poll_thread = threading.Thread(target=self.poll)
        poll_thread.daemon = True
        poll_thread.start()
        shell_thread = threading.Thread(target=self.shell)
        shell_thread.daemon = True
        shell_thread.start()
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
                mocp_state = subprocess.check_output(['mocp', '--info'])
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
        screen_w, screen_h = self.screen.get_size()
        screen_aspect = float(screen_w) / screen_h
        cover = pygame.image.load(album.cover).convert()
        cover_w, cover_h = cover.get_size()
        cover_aspect = float(cover_w) / cover_h
        if cover_aspect > screen_aspect:
            # Width is limiting factor, scale to width
            scale_w = screen_w
            scale_h = int(scale_w / cover_aspect)
        else:
            # Height is limiting factor, scale to height
            scale_h = screen_h
            scale_w = int(scale_h * cover_aspect)
        self.cover = pygame.transform.scale(cover, (scale_w, scale_h))
        self.draw()

    def jog(self, i):
        self.chill_out()
        if self.state == self.PAUSED:
            self.stop()
        if self.state == self.IDLE:
            self.set_album((self.album + i) % len(self.albums))
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
        self.draw()
        self.do(['mocp', '--stop'])

    def finish_play(self):
        self.state = self.IDLE
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
