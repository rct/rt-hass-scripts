#!/usr/bin/env python
#!/usr/bin/python
#
# Automate running Home Assistant backups from YAML config file
#
# TODO
# - Minimum python version?  will 3.8 work?
# - backup repr/str too verbose 
# - 
# - Compare output of folder and addons backup with Home Assistant Full Backup - anything missed?
#    Done?
# - What to do with backup output?  Save to hostname/slug.yaml? 
# - Get date of most recent backup by type 
#   - is it time to run a new backup?
#   - split up backups by type
#     get total size of backups on host 
# - How to run a specific backup, or a subset of backups
# - TBD get list of folders?  some default list of folders? 
# - Config checking (case?) 
# - Error handling, don't let one failed host make the rest fail.
# - should excluding an uninstalled addon be an error or not?

import sys, os, argparse, time, datetime, logging
import subprocess
import yaml
import typing

log = logging.getLogger("")


class HassInfo:
    """Accumulated info about Home Assisntant instance, list of addons, backups, etc. """

    # log = logging.getLogger(__name__)

    def __init__(self, name, hass_config = None) -> None:
        self.config = {}  # Dictionary defining a Hass instance and its scheduled backup
        self.name = name            # Name of Hass instance to use
        self.enabled = True         # Disable if commands fail and Support disabling in multi-host YAML
        self.disable_reason = None

        # ssh related arguments for running 'ssh -l user -p port hasshost ....
        self.host = None            # Host Hass is running on. 
        self.user = None            # ssh 
        self.sshport = 22
        self.source_profile = None  # path to profile to run to get Hass Auth Token
        
        # shell commands run on host 
        # constructed base command string to be able to run `ha`` on the right host
        # i.e: ['ssh' '-l' 'user' 'hostname' 'ha'] or [] if local.
        self.cmd_args = []          
        self.last_cmd_result = None # Results of last subprocess.run
        self.last_cmd_runtime = None   # Execution time in seconds of last command

        # Hass Host information
        self.ha_host_info = None
        self.disk_free_gb = None
        self.disk_free_pct = None

        # Hass ha backups to run (from YAML config file)
        self.backups_defined = []    # list of periodic backups to run
        self.backups_enabled = False

        # Hass previous backups (for figuring out when last backups were run)
        # Uses `ha backups` to get list of backups still on the host
        self.prev_backup_info = {}  # list of previous backups accessible by slug
        self.prev_backup_info_raw = None # saving raw response from `ha backups`

        # Hass ha addons info
        # Needed to populate list of addons to back up
        self.addons_info = {}       # Info indexed by addon slug
        self.addons_installed = []  # Sorted list of installed addons by slug
        self.addons_info_raw = None # saving raw response from `ha addons`

        if hass_config is not None:
            self.configure(hass_config)

        # TBD when to run 'ha info commands'?
        # load up front, or lazy when needed

    def configure(self, hass_config):
        """Extract Hass instance config from YAML dict"""
    
        self.config = hass_config
        

        if self.config.get('host'):
            self.host = hass_config['host']

        if self.config.get('user'):
            self.user = hass_config['user']

        if self.config.get('sshport'):
            self.sshport = hass_config['sshport']

        if self.config.get('source_needed'):
            self.source_profile = hass_config['source_needed']


        if self.host is not None and self.host != "localhost":
            self.cmd_args.append("ssh")
            if self.user is not None:
                self.cmd_args.append("-l")
                self.cmd_args.append(self.user)

            if self.sshport != 22:
                self.cmd_args.append("-p")
                self.cmd_args.append(self.sshport)

            self.cmd_args.append(self.host)

            # Check if sourcing profile is needed for auth token
            if self.source_profile is not None:
                self.cmd_args += [ 'source', self.source_profile, '&&']

        if self.config.get("backups"):
            for backup in self.config.get("backups"):
                self.backups_defined.append(HassBackup(self, backup))

            if len(self.backups_defined) > 0:
                self.backups_enabled = True

            log.debug(f"{self.name} backups {self.backups_defined}")

            
    
    def run_cmd(self, cmd):
        """run an 'ha ...' or other command on the Hass host
        
        subproces.run output is saved as last_cmd_results in the object"""

        run_cmd = self.cmd_args + cmd
        log.debug(f"Running cmd: {run_cmd}")

        time_start = time.time()
        self.last_cmd_result = subprocess.run(run_cmd, capture_output=True)
        
        self.last_cmd_runtime = time.time() - time_start
        log.debug(f"Cmd runtime: {self.last_cmd_runtime:.1f} seconds")
        log.debug(f"Cmd results {self.last_cmd_result}")

        # XXX check success, disable host if cmd fails
        # XXX not returing anything because output is saved as an instance variable

        # Invert exit value, so returns True if exit = 0 
        if self.last_cmd_result.returncode == 0:
            return True
        else:
            # Running host info failed, so disable host
            self.enabled = False
            self.disable_reason = "disabled: ha host info failed"
        
        return False


    def fetch_host_info(self):
        """run 'ha host info' """

        if self.run_cmd([ "ha", "host", "info" ]):
            self.ha_host_info = yaml.safe_load(self.last_cmd_result.stdout)
            self.disk_free_gb = self.ha_host_info.get("disk_free")
            self.disk_size = self.ha_host_info.get("disk_total")
            # XXX catch div/0? 
            self.disk_free_pct = (self.ha_host_info.get("disk_free") / self.disk_size) * 100.0

            # should this log here?  log at debug? 
            log.info(f"Host {self.name} disk free: {self.disk_free_pct:.1f}% ({self.disk_free_gb:.2f}GB of {self.disk_size:.1f}GB)")

            # Disable backups if < 2GB free
            if self.disk_free_gb < 2.0:
                log.critical(f"{self.name} backups disabled, free disk space: {self.disk_size:.1f}GB")
                self.backups_enabled = False

            log.debug(f"{self.name} host info {self.ha_host_info}")

            return True
        
        return False

    def fetch_addons_installed(self):
        """fetch the list of installed addons via `ha addons`"""

        # The command really should be ha addons info (or list?)
        # this will need to change if they ever make it more orthogonal
        if self.run_cmd([ "ha", "addons" ]):
            self.addons_info_raw = yaml.safe_load(self.last_cmd_result.stdout)
            addons_list = self.addons_info_raw["addons"]

            # sort by addon name for readability and consistency with Hass GUI
            addons_list = sorted(addons_list, key=lambda d: d['name'])

            # Keep sorted list of addon_slugs (by addon name for UX) to make iteration easier 
            self.addons_installed = { addon['slug'] for addon in addons_list }

            # a dict to Keep addon info accessible by slug
            self.addons_info = { addon['slug']:addon for addon in addons_list }

            log.debug(f"Addons: {self.name} installed list: {self.addons_installed}")

            return True
        
        return False
    
    def get_addons_installed(self):
        """Return dictionary of installed addon info by slug"""

        if not self.addons_info:
            self.fetch_addons_installed()

        return self.addons_info
    
    def get_prev_backup_info(self):
        """Get info about previous backups still on the host via `ha backups`
        
        To be used for figuring out date of liast backup for each component"""

        # like `ha addons` the root subcommand returns the list.
        if self.run_cmd([ "ha", "backups" ]):
            self.prev_backup_info_raw = yaml.safe_load(self.last_cmd_result.stdout)
            backup_list = self.prev_backup_info_raw["backups"]
            # keep previous backup info accessible by slug
            self.prev_backup_info = { backup["slog"]:backup for backup in backup_list }

            # @todo - figure out most recent backup by type 

            return True
        
        return False


    def run_backup(self, backup: 'HassBackup', dryrun = True, add_date = True):
        """Construct and run an `ha backups --name foo --folder bar --addon baz` cmd 
        
        Manual command lines for reference:
        # backup just home assistant
        ha backups new --name hass-ha-$(date +%Y-%m-%d) --folders homeassistant

        # backup home assistant ahd folder (GUI default for clicking Home Assistant)
        # list of folder is probably subject to change
        ha backups new --name hass-haf-$(date +%Y-%m-%d) --folders homeassistant \
            --folders addons/local --folders media --folders ssl --folders share 

        # backup a single addon
        ha backups new --name influxdb-$(date +%Y-%m-%d) --addons a0d7b954_influxdb

        # backup all addons, except InfluxDB
        ha backups new --name addons-haf-$( date +%Y-%m-%d ) \
            $( ha addon | sort | perl -ne 'print " --addons $1" if (/slug: (.*)$/ && $1 ne "a0d7b954_influxdb")' )


        """

        if not backup.enabled:
            log.info(f"Skipping disabled backup: {self.name} {backup.name}")
            return

        backup_name = backup.get_name()

        cmd_args = [ "ha", "backups", "new", "--name", backup_name, "--no-progress" ]

        cmd_args += backup.get_cli_args() 

        log.info(f"{self.name} Backup cmd {cmd_args}")

        if dryrun:
            return
        
        if self.run_cmd(cmd_args):
            result = yaml.safe_load(self.last_cmd_result.stdout)
            backup.slug = result["slug"]
            backup.cmd_runtime = self.last_cmd_runtime


            self.run_cmd([ "ha", "backups", "info", backup.slug ])
            backup.results = yaml.safe_load(self.last_cmd_result.stdout)
            size = backup.results["size"]

        
        # move?
        # Add to some more permanent log
        # Save backup info to {slug}.yaml? 
        log.info(f"Backup result {self.name}: {backup.name}, slug: {backup.slug} size: {size}MB, runtime: {backup.cmd_runtime:.1f} ")


    @staticmethod
    def parseyaml(yamlstr) -> list:
        """Parse Backup config file, return list of Hass objects"""

        # should this be a static for the class/or just a global 
        instances = {}

        try:
            config = yaml.safe_load(yamlstr)

        except yaml.YAMLError as exc:
            print("Error parsing backup config YAML file")
            print(exc)
            # XXX todo should return error
            sys.exit(-1)

        # @todo - parse any global settings 

        for hass in config.keys():
            instances[hass] = HassInfo(hass, config[hass])

        return instances

class HassBackup:
    """Encapsulate a Hass Backup from YAML config, run capture slug
    
    Supports include and exclude so that a backup spec could say
    include everything except `foo`

    Currently two types of backups:
    1. folders - relative to /mnt/data/supervisor or /usr/share/hassio
       Note: backing up folder `homeassistant` is the equivalent of the GUI's
       home assistant backup

    2. addons
    
    Currently installed addons to backup will be retrieved from `ha addson` output
    
    Only specifying exclude or include: ['*'] will auto-populate include with
    current list of addons
    
    Folder excludes don't make sense unless folder include '*' is implemented """

    def __init__(self, hass, bconfig) -> None:
        self.hass = hass
        self.config = bconfig
        self.name = bconfig.get("name")
        self.enabled = False    # Allow disabling from YAML or due to config issues
        self.folders_include = []       # folders to include Note: backing up folder 'homeassistant' equiv of GUI "Home Assistant"
        self.folders_exclude = []       # XXX currently not implemented, 
        self.addons_include = []        # addons to back up
        self.addons_exclude = []

        # Results of running a backup
        self.slug = ""
        self.results = {}

        if bconfig.get("enabled") == "false" or bconfig.get("enabled") == "False":
            return False

        self.enabled = bconfig.get("enabled", True)


        # Backup includes one or more folders
        if bconfig.get("folders"):
            self.folders_include = bconfig["folders"].get("include", [])
            self.folders_exclude = bconfig["folders"].get('exclude', [])    # XXX currently not used/implemented
            if bconfig["folders"].get('exclude'):
                raise NotImplementedError("Folder exclude not implemented")

        if bconfig.get("addons"):
            self.addons_include = bconfig["addons"].get("include", [])
            self.addons_exclude = bconfig["addons"].get("exclude", [])

    # tmp, delete
    def __repr__(self):
        return yaml.safe_dump(self.config)
        # return f"{self.name} folders inc: {self.folders_include} exc: {self.folders_include} " + f"addons inc: {self.addons_include} exc: {self.addons_exclude}"
    
    def get_name(self, add_date = True):
        """Name to use for backup"""

        today = datetime.datetime.now()
        backup_name = self.name
        if add_date: 
            backup_name += f"-{today:%Y-%m-%d}"

        return backup_name

    def get_folders(self):
        """Return list/set of folders to include in backup (Expands include/excludes) """

        folders = set()

        for f in self.folders_include:
            if f == "*":
                # @todo - add default folders
                # static list of homeassistant addons/local media ssl share ?
                # Or ssh to host and do a find?
                raise NotImplementedError("Folder include '*' not implemented yet")
            folders.add(f)

        if len(folders) == 0 and len(self.folders_exclude) > 0:
            raise NotImplementedError("Auto folder expansion not implemented yet")
        
        for f in self.folders_exclude:
            folders.remove(f)

        # Sorting for readability and consistency with Hass GUI
        if folders:
            folders = list(folders)
            folders.sort()

            # Make sure homeassistant is always first in the list
            if 'homeassistant' in folders:
                folders.remove('homeassistant')
                folders = ['homeassistant'] + folders

        return folders
    
    def get_addons(self):
        """Return list of addons to include in backup (Expands include/excludes)"""

        addons = set() # using a set to ensure no duplicates after include '*'/exclude

        installed_addons = self.hass.get_addons_installed()

        # only exclude specified, but no include, should this be an error?
        if len(self.addons_include) == 0 and len(self.addons_exclude) > 0:
            # Add all installed addons before exclude
            for addon in installed_addons:
                addons.add(addon)

        for f in self.addons_include:
            if f == "*":
                for addon in installed_addons:
                    addons.add(addon)
            else:
                addons.add(f)

        for f in self.addons_exclude:
            # XXX if the addon isn't in the list, an error will be thrown, should this be allowed?
            try:
                addons.remove(f)

            except KeyError:
                pass

        # Hass GUI sorts addons by name/title (not slug)
        sorted_addons = sorted(addons, key=lambda d: installed_addons[d]['name'])

        return sorted_addons

    def get_cli_args(self) -> list:
        """Return command line arguments to run a backup of the specified folders and addons
        
        Only returns the aguments, the ha command, and backup naming is left to be
        handled by the caller for flexibility"""

        cmd_args = []

        folders = self.get_folders()
        for folder in folders:
            cmd_args += [ "--folders", folder ]

        addons = self.get_addons()
        for addon in addons:
            cmd_args += [ "--addons", addon ]

        return cmd_args


def main():

    parser = argparse.ArgumentParser(description="Home Assistant Automated Backup")
    parser.add_argument('config_file', type=argparse.FileType('rt'))
    parser.add_argument('-y', '--yes', dest='run_backup', default=False, action='store_true', help="Run backups")
    parser.add_argument('-d', '--debug', dest='debug', default=False, action='store_true', help="Debug output")
    parser.add_argument('--dryrun', dest='run_backup', action='store_false', help="Do not run backups")

    # XXX DELETE vscode args workaround
    if len(sys.argv) == 1:
        sys.argv.append("hassio.yaml")

        log.info("No config file - Reading configuration from hasiso.yaml as a default")


    arg_results = parser.parse_args()
    arg_dryrun = not arg_results.run_backup
    
    # log = logging.getLogger("")
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s " + "[%(module)s:%(lineno)d] %(message)s"
    )
    # setup console logging
    log_level = logging.INFO
    if arg_results.debug:
        log_level = logging.DEBUG
    log.setLevel(log_level)
    ch = logging.StreamHandler()
    ch.setLevel(log_level)

    ch.setFormatter(formatter)
    log.addHandler(ch)


    # print(arg_results.config_file)
    log.debug("Starting")
    hass_configs = HassInfo.parseyaml(arg_results.config_file)

    # print("\n\nbackups:\n")
    # XXX TODO run backups
    for hasshostname, hasshost in hass_configs.items():
        # Check free disk space
        hasshost.fetch_host_info()

        # Find installed addons to backup
        # @todo this could/should be done lazily only if there are addons to backup
        # hasshost.get_addons_info()

        log.info(f"Starting backups for {hasshostname}")
        for backup in hass_configs[hasshostname].backups_defined:
            log.info(f"Starting backup {hasshostname}: {backup.name}")
            hass_configs[hasshostname].run_backup(backup, dryrun = arg_dryrun)
        
        # Re-run host info to get disk space after backup
        hasshost.fetch_host_info()


if __name__ == "__main__":
    main()
