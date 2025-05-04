# rt-hass-scripts
misc home assistant scripts

# WIP - Work in Progress

Quick stuff, minimal effort, intended to be useful to me.

Warning: Have not cleaned up/documented for publication

# hass-backup.py

Use the Hassio 'ha' CLI to automate backups

* driven by a  YAML config file
* will ssh to Hassio host and run `ha` commands
* can include/exclude addons and folders.
* Can be used to separate Hass backups from add-on backups.  Useful when add-ons have a lot of their own data like Unifi.

