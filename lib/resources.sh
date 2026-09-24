# The envelope and build budget for a bash caller, each function one call into lib/wk/resources.py; WK_MB_PER_JOB_EXPLICIT is whether a person set WK_MB_PER_JOB, so a config's figure does not replace theirs.

WK_MB_PER_JOB_EXPLICIT="${WK_MB_PER_JOB:+1}"   # before the default below lands

_res_py() {
    env PYTHONPATH="$WK_ROOT/lib" ${WK_STORE:+"WK_STORE=$WK_STORE"} ${WK_MARKER:+"WK_MARKER=$WK_MARKER"} \
        ${WK_RESERVE_CORES:+"WK_RESERVE_CORES=$WK_RESERVE_CORES"} ${WK_RESERVE_MB:+"WK_RESERVE_MB=$WK_RESERVE_MB"} \
        ${WK_HEADLESS_RESERVE_CORES:+"WK_HEADLESS_RESERVE_CORES=$WK_HEADLESS_RESERVE_CORES"} \
        ${WK_HEADLESS_RESERVE_MB:+"WK_HEADLESS_RESERVE_MB=$WK_HEADLESS_RESERVE_MB"} \
        ${WK_MB_PER_JOB:+"WK_MB_PER_JOB=$WK_MB_PER_JOB"} ${WK_MAX_JOBS:+"WK_MAX_JOBS=$WK_MAX_JOBS"} \
        ${WK_LOAD:+"WK_LOAD=$WK_LOAD"} ${WK_CGROUP_CORES:+"WK_CGROUP_CORES=$WK_CGROUP_CORES"} \
        ${WK_CGROUP_MB:+"WK_CGROUP_MB=$WK_CGROUP_MB"} ${WK_AVAIL_MB:+"WK_AVAIL_MB=$WK_AVAIL_MB"} \
        ${WK_BUILD_MACHINE:+"WK_BUILD_MACHINE=$WK_BUILD_MACHINE"} ${WK_BUILD_DISK_GB:+"WK_BUILD_DISK_GB=$WK_BUILD_DISK_GB"} \
        python3 -m wk.resources --os "$(wk_os)" "$@"
}

eval "$(_res_py defaults)"

headless_marker() { _res_py headless-marker; }
host_cores()      { _res_py host-cores; }
host_mem_mb()     { _res_py host-mem-mb; }
host_load()       { _res_py host-load; }      # whole cores, which build_jobs polite subtracts
envelope_cores()  { _res_py envelope-cores; }   # the cap on the VM (macOS) or container (Linux)
envelope_mem_mb() { _res_py envelope-mem-mb; }
describe_cores()  { _res_py describe-cores; }
build_record()    { _res_py build-record "$@"; }   # <label> <jobs> <budget-mb> <holder: pid:<n> or ws:<name>:<pidfile>>
disk_admit()      { _res_py disk-admit "$@"; }     # <what> [need-gb]
build_admit()     { _res_py build-admit "$@"; }    # <what> <jobs> [disk-gb] -- one machine builds one thing at a time
build_jobs()      { _res_py build-jobs "$@"; }     # [polite]
