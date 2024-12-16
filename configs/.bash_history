rustup
cargo
LV_BRANCH='release-1.4/neovim-0.9' bash <(curl -s https://raw.githubusercontent.com/LunarVim/LunarVim/release-1.4/neovim-0.9/utils/installer/install.sh)
nvim
lvim
source init-debug
nvim
lvim
source init-debug
git status
git diff
cd /WebKit/buildroot/
git status
ls arm
ls arm/arm
rm arm
sudo rm arm
git stash
sudo git fetch
sudo chown -R justin .
git fetch
git pull
ssh arm
source init-debug
git status
source init-release
git status
git checkout main && git pull
git status
scp arm:~/BBQFix.patch .
scp arm:~/BBQ*.patch .
git apply BBQFIX.patch
rm BBQFIX.patch 
git status
git-webkit pr
ssh arm
source init-debug
git status
git diff > /WebKit/patches/DisablePSONandFixStrongCycleInWI.patch
git diff
git stash
git pull
git status
git fetch justinmichaud
git fetch fork
git fetch origin
git status
git rebase --abort
git status
git log
git show
git reset --hard fork/eng/Add-strong-root-logging-and-vm-helpers-for-memory-leak-debugging
git status
rm update-compile-commands-symlink.conf 
jscb
source init-releaseg
source init-release
git status
git pull
git status
git commit -a --amend
git push -f
git fetch justinmichaud
git fetch fork
git checkout eng/armv7-remove-b3-tmppair
git pull
git status
git log
git status
git commit -a --amend
git push -f
git checkout main && git pull
git checkout eng/armv7-remove-b3-tmppair
git pull
git statsu
git status
git commit -a --amend
git status
git push -f
git commit -a --amend
git push -f
git status
cd /WebKit
ls
ls tmp
ls Yokto/
sudo dnf install qemu qemu-img python3 python3-pip
git clone git@github.com:foxlet/macOS-Simple-KVM.git
cd macOS-Simple-KVM/
ls
qemu-img create -f qcow2 MyDisk.qcow2 64G
lvim basic.sh 
./jumpstart.sh 
sudo ./make.sh --add
source init-release
git status
git fetch fork
git checkout eng/Add-strong-root-logging-and-vm-helpers-for-memory-leak-debugging
git pull
git status
cd /WebKit/macOS-Simple-KVM/
setfacl -m u:qemu:rwx ../ 
setfacl -m u:libvirt:rwx ../ 
ls -al
chmod -R 777 .
suod chmod -R 777 .
sudo chmod -R 777 .
ls -al
chcon -Rt svirt_sandbox_file_t .
sudo chcon -Rt svirt_sandbox_file_t .
./basic.sh
ls
rm -rf MyDisk.qcow2 
git status
rm template.xml 
git status
git checkout .
qemu-img create -f qcow2 MyDisk.qcow2 128G
lvim basic.sh 
./jumpstart.sh -h
git status
rm BaseSystem.img 
git status
cd ..
rm -rf macOS-Simple-KVM/
git clone git@github.com:notAperson535/OneClick-macOS-Simple-KVM.git
cd OneClick-macOS-Simple-KVM/
./setupFedora.sh 
./basic.sh 
ls
sudo ./make.sh --add
ssh root@buildroot
source init-debug
git statuss
git status
source init-release
git status
lvim
git status
git commit -a --amend
git push -f
git push -f --set-upstream fork eng/Fix-ARMv7-clang-build
git status
lvim
git status
git commit -a --amend
git push -f
ssh root@buildroot
git status
source init-debug
git status
git apply ~/Desktop/tmp.patch
git apply ~/Desktop/tmp.patch -4
git apply ~/Desktop/tmp.patch -3
git status
git checkout Source/cmake/OptionsWPE.cmake
git restore --staged Source/cmake/OptionsWPE.cmake
git checkout Source/cmake/OptionsWPE.cmake
git reset .
git checkout -p
git status
jscb
git status
jscb
git status
git log
git fetch fork
git status
git add .
git commit -m '.'
git fetch origin
git rebase -i origin/main
git push f-
git push -f
git status
git commit -a --amend
git push -f
git status
build-webkit --debug --gtk --enable-compile-commands
wkdev-enter --name wkdev
cd /WebKit/buildroot/
ls
git status
git log
git status
sudo rm -rf output
sudo tar -xvpsf output/images/rootfs.tar -C /run/media/justin/rootfs/ --unlink-recursive
sudo tar -xvpsf output/images/rootfs.tar -C /run/media/justin/rootfs/ --overwrite-dir --recursive-unlink
sync
sudo umount /run/media/justin/*
ssh root@buildroot
ssh buildroot
sudo chown justin package/cog/cog.mk
sudo rm -rf /run/media/justin/rootfs/*
sudo tar -xvpsf output/images/rootfs.tar -C /run/media/justin/rootfs/ --overwrite-dir --recursive-unlink
sync
sudo umount /run/media/justin/*
ssh buildroot
cd ~/Downloads/
ls
python3 -m SimpleHTTPServer
[200~python -m http.server 8000~
python -m http.server 8000
ifconfig
python -m http.server 8000
rm Xcode_15.4.xip
man ln
ln -s /WebKit/ReleaseVersion/ ~/Public/ReleaseVersion
chcon -Rt svirt_sandbox_file_t /WebKit/OneClick-macOS-Simple-KVM/
sudo chcon -Rt svirt_sandbox_file_t /WebKit/OneClick-macOS-Simple-KVM/
nvim /WebKit/OneClick-macOS-Simple-KVM/template.xml 
sudo nvim /WebKit/OneClick-macOS-Simple-KVM/template.xml 
cd /WebKit/OneClick-macOS-Simple-KVM/
nvim basic.sh 
./basic.sh 
git checkout -p
./basic.sh 
sudo dnf install libfsapfs-utils
sudo dnf install libfsapfs
sudo dnf install fsapfs
sudo dnf install libfsapfs-tools
lsusb
vi basic.sh 
cd /run/media/justin/WebKitStick/ReleaseVersion/OpenSource/
git status
git checkout .
git status
git config core.filemode false
git status
git pull
git status
cd /WebKit/OneClick-macOS-Simple-KVM/
./basic.sh 
lsusb
./basic.sh 
vi basic.sh 
./basic.sh 
sudo ./basic.sh 
vi basic.sh 
sudo ./basic.sh 
lsusb
vi basic.sh 
sudo ./basic.sh 
lsusb
vi basic.sh 
sudo ./basic.sh 
ssh arm
cd /WebKit/Yokto/OpenSource/
git status
git diff
git checkout Source/cmake/OptionsCommon.cmake 
docker run -ti --rm -v /WebKit/buildroot:/root/buildroot -v /WebKit/Yokto:/root/Yokto mcr.microsoft.com/devcontainers/base:ubuntu-22.04 /bin/bash
cd /WebKit/buildroot/
sudo chown justin package/rpi-firmware/rpi-firmware.hash 
git status
git diff
cp configs/raspberrypi3_wpe_2_46_cog_defconfig configs/raspberrypi3_wpe_2_46_cog_libbacktrace_defconfig
sudo cp configs/raspberrypi3_wpe_2_46_cog_defconfig configs/raspberrypi3_wpe_2_46_cog_libbacktrace_defconfig
sudo chown justin configs/raspberrypi3_wpe_2_46_cog_libbacktrace_defconfig
git status
git diff
git checkout /package/cog/cog.mk
git checkout package/cog/cog.mk
sudo git checkout package/cog/cog.mk
git stauts
git status
git diff
vi .config
mv .config .config.workingbacktrace
sudo mv .config .config.workingbacktrace
sudo vi local.mk 
sudo chown justin configs/raspberrypi3_wpe_2_46_cog_defconfig
git status
cd /WebKit/Yokto/OpenSource/
git status
git diff
git diff > /WebKit/patches/WebKitToTUseBacktraceAndLogStrongsOld.patch
cd /WebKit/buildroot/
sudo chown justin package/wpe/wpewebkit/wpewebkit.mk
git status
diff .config .config.workingbacktrace 
git status
mv .config.workingbacktrace ..
mv .config.workingbacktrace /WebKit
sudo chown justin /WebKit
mv .config.workingbacktrace /WebKit
mv .config.workingbacktrace /WebKit/
sudo mv .config.workingbacktrace /WebKit/
git status
git diff
cd /WebKit/Yokto/OpenSource/
git status
git diff
git checkout -p
git status
git checkout Source/cmake/OptionsWPE.cmake
git status
git stash
git checkout wpe-2.46
git remote -v
git fetch wpe
git checkout wpe-2.46
git stash apply
git diff
cd ../../buildroot/
git status
git fetch fork
sudo chown -R justin .
sudo chown -R justin .git
git fetch fork
git status
sudo chown -R justin configs
sudo chown -R justin package
git commit
git branch
git branch eng/update-rpi3-firmware-and-use-reftracker
git push -u fork eng/update-rpi3-firmware-and-use-reftracker
source init-release
git status
git diff
git stash
git checkout main && git pull
git stash apply
git status
git-webkit pr
cd /WebKit/Yokto/OpenSource/
git status
git diff
git status
source init-release
git status
git diff
git commit -a --amend
git push -f
git push -f --set-upstream fork eng/Use--funwind-tables-when-LIBBACKTRACE-is-enabled-
cd /WebKit/Yokto/OpenSource/
git status
source init-release
git status
git commit -a --amend
git push -f
ssh arm
cd /WebKit/Yokto/OpenSource/
git status
git checkout -p
git status
git diff
git diff > ~/Desktop/tmp.patch
ssh buildroot
source init-debug
git status
git diff
rm oom 
git stash
git pull
nc -l 12345 | tar -xf -
ls
nc -l 12345 | tar -xf -
systemctl stop firewalld.service
nc -l 12345 | tar -xf -
ls
git status
git apply Users/justinmichaud/Desktop/diff.patch 
rm -rf Users/
git status
git add -a --amend
git log
git commit -a --amend
git push -f
whoami
wkdev-enter --name wkdev
source init-release
git status
git pull
git commit --amend --reword
git commit --amend
git push -f
sudo dnf install gnome-shell-extension-pop-shell xprop
wkdev-enter --name wkdev
flatpak ps
wkdev-enter --name wkdev
wkdev-enter -h
wkdev-enter --name wkdev
ssh arm
source init-debug
git status
source init-release
git status
git checkout main && git pull
git status
scp arm:~/DebugVersion/OpenSource/diff.patch .
scp arm:~/DebugVersion/OpenSource/diff* .
git status
git apply diff.txt
git apply diff.txt -3
rm diff.txt 
git status
git-webkit pr
ssh arm
source init-debug
source init-release
scp arm:~/DebugVersion/OpenSource/diff* .
git apply -3 diff.txt
git status
rm diff.txt 
git diff
git status
git add .
git reset .
git checkout -p
git statsu
git status
git commit --amend
git commit --amend -a
git push -f
git push -f --set-upstream fork eng/ARMv7-Return-exceptions-from-operations-using-long-long
git status
git commit -a --amend
git push -f
git status
git commit -a --amend
git push -f
git commit -a --amend
git push -f
jscb
git status
git commit -a --amend
git push -f
git status
jscb
git status
git commit -a --amend
git push -f
ssh arm
source init-release
git status
git pull
git status
git commit -a --amend
git push -f
git status
git checkout main && git pull
scp arm:~/DebugVersion/OpenSource/diff* .
git apply -3 diff.txt
git status
rm diff.txt 
git reset .
git diff
jscb
ssh arm
jscr JSTests/stress/instanceof-proxy.js  --useDollarVM=1 --useDFGJIT=0 --useConcurrentJIT=0 --jitAllowList=foo --dumpDisassembly=1 --forceICFailure=1
git status
jscb
jscr JSTests/stress/instanceof-proxy.js  --useDollarVM=1 --useDFGJIT=0 --useConcurrentJIT=0 --jitAllowList=foo --dumpDisassembly=1 --forceICFailure=1
git status
git diff
git add .
git-webkit pr
ssh arm
source init-release
git status
git checkout main
scp arm:~/DebugVersion/OpenSource/diff* .
git apply -3 diff.txt
rm diff.txt 
git-webkit pr
source init-release
git status
git fetch fork
git checkout eng/ARMv7-Return-exceptions-from-operations-using-long-long
git pull
git fetch origin
git rebase origin/main
git status
git add .
git rebase --continue
git push -f
git log
source init-release
git status
git commit -a --amend
wkdev-enter
ssh arm
source init-release
git status
git checkout main && git pull
scp arm:~/DebugVersion/OpenSource/diff* .
git apply -3 diff.txt
rm diff.txt 
git status
git-webkit pr
cd /WebKit/buildroot/
git status
git log
git show
sudo rm -rf output
source init-debug
git pull
ssh buildroot
cd /WebKit/Yokto/OpenSource/
git status
git diff
ssh buildroot
git diff
ssh buildroot
git status
git diff
ssh arm
ssh buildroot
git status
git commit -a --amend
git push -f
ssh buildroot
vi Tools/lldb/lldb_webkit.py
ssh buildroot
sudo dnf install deskreen
sudo dnf install desksreen
ssh buildroot
docker run -ti --rm -v /WebKit/buildroot:/root/buildroot -v /WebKit/Yokto:/root/Yokto mcr.microsoft.com/devcontainers/base:ubuntu-22.04 /bin/bash
source init-debug
git status
git fetch
git commit -a --amend
git push -f
cd ../../buildroot/
ls -al /usr/lib | grep WPE
ls -al /usr/lib
ls -al /usr/lib/
ls -al /usr/lib/ | grep -i wpe
ls -al /usr/lib/ | grep -i lib
scp  -o UserKnownHostsFile=/dev/null -O output/target/usr/lib/libWPEWebKit-1.1.so.0.8.3  buildroot:/usr/lib
source init-debug
git status
git commit -a --amend
git push -f
git status
git commit -a --amend
git push -f
cd ../../buildroot/
scp  -o UserKnownHostsFile=/dev/null -O output/target/usr/lib/libWPEWebKit-1.1.so.0.8.3  buildroot:/usr/lib
git status
git diff
git status
git fetch
git status
git log
git commit
git commit -a
git push -f
wkdev-enter
scp  -o UserKnownHostsFile=/dev/null -O output/target/usr/lib/libWPEWebKit-1.1.so.0.8.3  buildroot:/usr/lib
source init-debug
git status
git pull
git fetch origin
git status
git fetch origin
git fetch
git status
git commit -a --amend
git rebase origin/main
git push -0f
git push -f
cd ../../buildroot/
source init-debug
git status
git commit -a --amend
git push -f
cd ../../buildroot/
scp  -o UserKnownHostsFile=/dev/null -O output/target/usr/lib/libWPEWebKit-1.1.so.0.8.3  buildroot:/usr/lib
source init-release
git status
git checkout main
git pull
git status
git-webkit pr
source init-debug
git status
git checkout main && git pull
jscb
cd JSTests/wasm/stress/
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0
jscb
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=1 --dumpDisassembly=1
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=1 --dumpDisassembly=1 --useConcurrentJIT=0
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --collectContinuously=1
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPaths=1
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
python
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
git status
cd ../../../
git stash
git pull
git stash apply
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0
cd JSTests/wasm/stress/
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscbreset
reset
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0 --breakOnThrow=1
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0 --breakOnThrow=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0 --breakOnThrow=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0 --breakOnThrow=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0 --breakOnThrow=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0 --breakOnThrow=1
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0 --breakOnThrow=1
git status
git stash
git stash apply
git checkout -p
git diff
jscb
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --traceLLIntExecution=0 --traceLLIntSlowPath=0 --breakOnThrow=0
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
rr
cp /WebKit/wk-tools/jscd /WebKit/wk-tools/jscrr
vi `which jscrr`
jscrr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
vi `which jscrr`
jscrr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
echo "kernel.perf_event_paranoid = 1" >> /etc/sysctl.d/10-rr.conf
sudo bash -c "echo "kernel.perf_event_paranoid = 1" >> /etc/sysctl.d/10-rr.conf"
sudo bash -c "echo \"kernel.perf_event_paranoid = 1\" >> /etc/sysctl.d/10-rr.conf"
'man 5 sysctl.d
man 5 sysctl.d
/proc/sys/kernel/perf_event_paranoid=1
sudo sysctl kernel.perf_event_paranoid=1
jscrr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
sudo dnf remove rr
cd /WebKit/
sudo dnf install   ccache cmake make gcc gcc-c++ gdb lldb libgcc libgcc.i686   glibc-devel glibc-devel.i686 libstdc++-devel libstdc++-devel.i686 libstdc++-devel.x86_64   python3-pexpect man-pages ninja-build capnproto capnproto-libs capnproto-devel   zlib-devel libzstd-devel
git clone https://github.com/rr-debugger/rr.git
cd rr/
mkdir obj
cd obj/
CC=clang CXX=clang++ cmake -G Ninja -DCMAKE_BUILD_TYPE=Release ../rr
cd ..
cd obj/
CC=clang CXX=clang++ cmake -G Ninja -DCMAKE_BUILD_TYPE=Release ../
cmake --build .
sudo cpupower frequency-set -g performance
sudo cmake --build . --target install
rr
ssh arm
source init-debug
git status
source init-release
git status
git checkout main && git pull
scp arm:~/DebugVersion/OpenSource/diff* .
git apply diff.txt
git status
rm diff.txt 
git diff
git-webkit pr
ssh arm
git status
git commit -a --amend
git push -f
git push -f --set-upstream fork 
source init-debug
git status
git pull
git fetch origin
git status
git diff
git commit -a --amend
git rebase origin/main
git push -f
ssh arm
cat ~/.lldb/lldb-widehistory 
jupyter
pip3 install jupyterlab
jupyter lab
git status
cd ~
ls
sudo pip3 install numpy matplotlib
sudo pip3 install scipy chebyshev
sudo pip3 install scipy
pip3 install scipy
jupyter lab
DISPLAY= /opt/google/chrome-remote-desktop/start-host --code="4/0AVG7fiQ2_bDEvxBxnPff_yDdCot5Pu-5gunoOhGzWZBt2lj__6gFzRejIQadWJx1D8zE8g" --redirect-url="https://remotedesktop.google.com/_/oauthredirect" --name=$(hostname)
whereis google-chrome
whereis chromium
source init-release
git status
git checkout wpe-2.46
git remote add wpe git@github.com:WebPlatformForEmbedded/WPEWebKit.git
git fetch wpe
git checkout wpe-2.46
git pull
git branch -b jmichaud/wpe-2.46-memory-helpers
git branch jmichaud/wpe-2.46-memory-helpers
git cherry-pick 7db7c2ae79a3a180ad03e10b71aed511eae4224b
git cherry-pick 41dc316f40f48a3a1394f1ef811839390d06e75c
git status
git add .
git cherry-pick --continue
git status
git push
git status
git log
source init-debug
cd JSTests/wasm/stress/
jscrr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
sudo dnf install chrome-remote-desktop
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscrr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscrrr
jscb
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscrr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscrrr
jscb
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscrr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscrrr
jscb
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscrr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscrrr
jscb
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscb
jscd -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscrr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscrrr
jscb
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
cd ../../../
git status
git checkout -p
git status
git diff
git status
git checkout Source/WTF/wtf/PlatformEnable.h
git diff
jscb
cd JSTests/wasm/stress/
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
cd ../../../
git status
git-webkit pr
run-javascriptcore-tests -h
run-jsc-stress-tests --quick --debug --no-build -h
run-jsc-stress-tests --quick --debug --no-build JSTests/wasm.yaml 
run-jsc-stress-tests --quick --debug JSTests/wasm.yaml 
run-jsc-stress-tests --quick --debug JSTests/wasm.yaml --jsc WebKitBuild/JSCOnly/Debug/bin/jsc -h
run-jsc-stress-tests --quick --debug JSTests/wasm.yaml --jsc WebKitBuild/JSCOnly/Debug/bin/jsc --env-vars "JSC_useJIT=0"
run-jsc-stress-tests --quick --debug --jsc WebKitBuild/JSCOnly/Debug/bin/jsc --env-vars "JSC_useJIT=0" JSTests/wasm.yaml
git status
run-jsc-stress-tests --quick --debug --jsc WebKitBuild/JSCOnly/Debug/bin/jsc --env-vars "JSC_useJIT=0" JSTests/wasm.yaml --filter ".*(wasm-imports|wasm-cycle|wasm-js|anyfunc|multi-value|table-access|indirect-calls|wasm-math-intrinsic|user-properties|default-import|i31|cc-int-to-int-tail-call|cc-i64|tail-call-simple).*"
cd JSTests/wasm/stress/
jscr cc-i64-kitchen-sink.js 
jscr cc-i64-kitchen-sink.js -m
jscr cc-i64-kitchen-sink.js -m --useJIT=0
jscr cc-i64-kitchen-sink.js -m --useJIT=1
jscr cc-i64-kitchen-sink.js -m --useJIT=0
jscd cc-i64-kitchen-sink.js -m --useJIT=0 --breakOnThrow=1
jscb
jscd cc-i64-kitchen-sink.js -m --useJIT=0 --breakOnThrow=1
jscr cc-i64-kitchen-sink.js -m --useJIT=0 --breakOnThrow=0
jscr cc-i64-kitchen-sink.js -m --useJIT=1
jscr cc-i64-kitchen-sink.js -m --useJIT=1 --useBBQJIT=0 --useOMGJIT=0
jscr cc-i64-kitchen-sink.js -m --useJIT=1 --useBBQJIT=0 --useOMGJIT=0 --jitAllowList=no
jscd cc-i64-kitchen-sink.js -m --useJIT=0 --breakOnThrow=1
jscb
jscd cc-i64-kitchen-sink.js -m --useJIT=0 --traceLLIntExecution=1 --traceLLIntSlowPath=1
jscr cc-i64-kitchen-sink.js -m --useJIT=0 --traceLLIntExecution=0 --traceLLIntSlowPath=1
jscb
jscr cc-i64-kitchen-sink.js -m --useJIT=0 --traceLLIntExecution=0 --traceLLIntSlowPath=1
jscr cc-i64-kitchen-sink.js -m --useJIT=0
jscb
jscr cc-i64-kitchen-sink.js -m --useJIT=0
jscb
jscr cc-i64-kitchen-sink.js -m --useJIT=0
git status
git diff
cd ../../../
git status
git checkout  JSTests/wasm/stress/cc-i64-kitchen-sink.js
git add .
git commit -m '.'
git push
git status
git push --set-upstream fork/eng/Fix-wasm-JS-when-useJIT0
git fetch fork
git push --set-upstream fork/eng/Fix-wasm-JS-when-useJIT0
git push --set-upstream fork eng/Fix-wasm-JS-when-useJIT0
git status
git reset HEAD~2
git status
git checkout -p
git status
jscb
git status
jscb
git status
git checkout .
git checkout eng/Fix-wasm-JS-when-useJIT0
git pull
git reset HEAD~2
git status
git checkout -p
git status
git diff
git checkout Source/JavaScriptCore/wasm/WasmSlowPaths.cpp
jscb
cd JSTests/wasm/stress/
jscr cc-i64-kitchen-sink.js -m --useJIT=0
git status
git diff
jscb
jscr cc-i64-kitchen-sink.js -m --useJIT=0
git status
git stash
jscb
jscr cc-i64-kitchen-sink.js -m --useJIT=0
git checkout eng/Fix-wasm-JS-when-useJIT0
git pull
jscb
cd ../../../
run-jsc-stress-tests --quick --debug --jsc WebKitBuild/JSCOnly/Debug/bin/jsc --env-vars "JSC_useJIT=0" JSTests/wasm.yaml --filter ".*(wasm-imports|wasm-cycle|wasm-js|anyfunc|multi-value|table-access|indirect-calls|wasm-math-intrinsic|user-properties|default-import|i31|cc-int-to-int-tail-call|cc-i64|tail-call-simple).*"
run-jsc-stress-tests --quick --debug --jsc WebKitBuild/JSCOnly/Debug/bin/jsc --env-vars "JSC_useJIT=0" JSTests/wasm.yaml --filter ".*(wasm-imports|wasm-cycle|wasm-js|anyfunc|multi-value|table-access|indirect-calls|wasm-math-intrinsic|user-properties|default-import|i31|cc-int-to-int-tail-call|cc-i64|tail-call-simple).*" -vvv
cd JSTests/wasm/stress/
jscr wasm-js-multi-value-exception-in-iterator.js
jscr wasm-js-multi-value-exception-in-iterator.js -m
jscr wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscd wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscrr wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscrrr
git status
git diff
jscb
jscr wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscb
jscr wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscd wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscb
jscd wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscrr wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscrrr
jscb
jscr wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscb
jscr wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscrr wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscrrr
jscb
jscr wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscrr wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscrrr
jscb
jscr wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
jscd wasm-js-multi-value-exception-in-iterator.js -m --useJIT=0
cd ../../../
git status
git commit -a -m '.'
git push
system76-power charge-thresholds -h
system76-power charge-thresholds --profile balanced
system76-power charge-thresholds --profile full_charge
source init-debug
git status
git fetch origin
git fetch fork
git pull
git rebase origin/main
git status
jscb
cd JSTests/wasm/stress/
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --useWebAssembly=1
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --useWasm=1
jscb
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --useWasm=1
jscrr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --useWasm=1
jscrrr
jscb
jscr -m cc-int-to-int-cross-module-with-exception.js --useJIT=0 --useGC=0 --useWasm=1
 git status
git diff
cd ../../../
git commit -a --amend
git rebase -i origin/main
git push -f
killall ssh
ssh arm
source init-release
git status
git checkout main && git pull
git status
scp arm:~/DebugVersion/OpenSource/diff* .
scp arm:~/diff* .
git apply diff.txt
git apply diff.txt -3
git status
rm diff.txt 
git status
git add .
git-webkit pr
git status
ssh arm
git status
git checkout main
git pull
scp arm:~/DebugVersion/OpenSource/diff* .
git apply diff.txt
git status
rm diff.txt 
git-webkit pr
sudo apt remove google-chrome*
sudo dnf remove google-chrome*
