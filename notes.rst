
Power Supply
------------

Hammond 270X or 270CAX
240VAC or 250VAC, 5V 2A, 6.3V 1.5A or 2.5A

Run logic (Raspberry PI, Arduino) from 5V
TV from 6.3V

Amplifier
---------

5902 Power Tubes PP
Hammond 125J Output transformer

One speaker possibility:
http://www.crutchfield.com/p_113KFC1093/Kenwood-KFC-1093PS.html?tp=102#details-tab

Software
--------

Install Raspbian "Wheezy":

http://www.raspberrypi.org/downloads

(Used 2012-12-16)

At startup, expanded filesystem to fill disk, and turned on sshd, and install 
upgrades.

Then login as pi/raspberry and setup wireless by editing 
/etc/network/interfaces::

    auto lo

    iface lo inet loopback
    iface eth0 inet dhcp

    allow-hotplug wlan0
    iface wlan0 inet static
    wpa-ssid archimedean
    wpa-psk XXXX
    address 192.168.2.4
    netmask 255.255.255.0
    gateway 192.168.2.1

    iface default inet dhcp

Then restart.

Install following packages::

    moc
    moc-ffmpeg-plugin
    sshfs

USB sound interface needs to be default, since moc only works with default alsa
device.  Make ~/.asoundrc look like this::

    pcm.!default {
        type hw
        card 1
    }
    ctl.!default {
        type hw           
        card 1
    }

Set hostname::

    $ sudo hostname liljuke

Add siracusa to /etc/hosts::

    192.168.2.5	siracusa
    
Run ssh-keygen:

    $ ssh-keygen

Add ~/.ssh/id_rsa.pub to ~/.ssh/authorized keys on chris@siracusa.

Add siracusa to ~/.ssh/config::

    Host *
    ForwardAgent yes
    ForwardX11 no
    Compression yes
    StrictHostKeyChecking no
    ServerAliveInterval 300

    Host siracusa
      User chris
      HostName 192.168.2.5

Add sshfs for siracusa to /etc/fstab::

    sshfs#chris@siracusa:Music /home/pi/music fuse comment=sshfs,noauto,users,uid=1000,gid=1000,reconnect,BatchMode=yes 0 0

Add `pi` user to `fuse` group::

    $ sudo usermod -a -G fuse pi

Log out and log back in for change to take effect.  In /etc/fuse.conf,
uncomment `user_allow_other` line.

Create /etc/network/if-up.d/mountsshfs::

    #!/bin/sh

    ## http://ubuntuforums.org/showthread.php?t=430312
    ## The script will attempt to mount any fstab entry with an option
    ## "...,comment=$SELECTED_STRING,..."
    ## Use this to select specific sshfs mounts rather than all of them.
    SELECTED_STRING="sshfs"

    # Not for loopback
    [ "$IFACE" != "lo" ] || exit 0

    ## define a number of useful functions

    ## returns true if input contains nothing but the digits 0-9, false otherwise
    ## so realy, more like isa_positive_integer 
    isa_number () {
        ! echo $1 | egrep -q '[^0-9]'
        return $?
    }

    ## returns true if the given uid or username is that of the current user
    am_i () {
        [ "$1" = "`id -u`" ] || [ "$1" = "`id -un`" ]
    }

    ## takes a username or uid and finds it in /etc/passwd
    ## echoes the name and returns true on success
    ## echoes nothing and returns false on failure 
    user_from_uid () {
        if isa_number "$1"
        then
            # look for the corresponding name in /etc/passwd
            local IFS=":"
            while read name x uid the_rest
            do
                if [ "$1" = "$uid" ]
                then 
                    echo "$name"
                    return 0
                fi
            done </etc/passwd
        else
            # look for the username in /etc/passwd
            if grep -q "^${1}:" /etc/passwd
            then
                echo "$1"
                return 0
            fi
        fi
        # if nothing was found, return false
        return 1
    }

    ## Parses a string of comma-separated fstab options and finds out the 
    ## username/uid assigned within them. 
    ## echoes the found username/uid and returns true if found
    ## echoes "root" and returns false if none found
    uid_from_fs_opts () {
        local uid=`echo $1 | egrep -o 'uid=[^,]+'`
        if [ -z "$uid" ]; then
            # no uid was specified, so default is root
            echo "root"
            return 1
        else
            # delete the "uid=" at the beginning
            uid_length=`expr length $uid - 3`
            uid=`expr substr $uid 5 $uid_length`
            echo $uid
            return 0
        fi
    }

    # unmount all shares first
    sh "/etc/network/if-down.d/umountsshfs"

    while read fs mp type opts dump pass extra
    do
        # check validity of line
        if [ -z "$pass" -o -n "$extra" -o "`expr substr ${fs}x 1 1`" = "#" ]; 
        then
            # line is invalid or a comment, so skip it
            continue
        
        # check if the line is a selected line
        elif echo $opts | grep -q "comment=$SELECTED_STRING"; then
            
            # get the uid of the mount
            mp_uid=`uid_from_fs_opts $opts`
            
            if am_i "$mp_uid"; then
                # current user owns the mount, so mount it normally
                { sh -c "mount $mp" && 
                    echo "$mp mounted as current user (`id -un`)" || 
                    echo "$mp failed to mount as current user (`id -un`)"; 
                } &
            elif am_i root; then
                # running as root, so sudo mount as user
                if isa_number "$mp_uid"; then
                    # sudo wants a "#" sign icon front of a numeric uid
                    mp_uid="#$mp_uid"
                fi 
                { sudo -u "$mp_uid" sh -c "mount $mp" && 
                    echo "$mp mounted as $mp_uid" || 
                    echo "$mp failed to mount as $mp_uid"; 
                } &
            else
                # otherwise, don't try to mount another user's mount point
                echo "Not attempting to mount $mp as other user $mp_uid"
            fi
        fi
        # if not an sshfs line, do nothing
    done </etc/fstab

    wait

Create /etc/network/if-down.d/umountsshfs::

    #!/bin/bash

    # Not for loopback!
    [ "$IFACE" != "lo" ] || exit 0

    # comment this for testing
    exec 1>/dev/null # squelch output for non-interactive

    # umount all sshfs mounts
    mounted=`grep 'fuse.sshfs\|sshfs#' /etc/mtab | awk '{ print $2 }'`
    [ -n "$mounted" ] && { for mount in $mounted; do umount -l $mount; done; }

Make sure root can execute::

    sudo chmod 755 /etc/network/if-up.d/mountsshfs /etc/network/if-down.d/umountsshfs
    sudo chown root:root /etc/network/if-up.d/mountsshfs /etc/network/if-down.d/umountsshfs
