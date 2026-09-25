pid=$(cat /run/wk-bench-seat.pid 2>/dev/null)
[ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && exit 0
setsid python3 -c 'import fcntl,os,struct,signal
IOC=lambda d,t,nr,size:(d<<30)|(size<<16)|(t<<8)|nr
U=ord("U")
fd=os.open("/dev/uinput",os.O_WRONLY|os.O_NONBLOCK)
fcntl.ioctl(fd,IOC(1,U,100,4),1)
fcntl.ioctl(fd,IOC(1,U,101,4),1)
os.write(fd,struct.pack("=80s4HI256i",b"wk-bench-seat",3,1,1,1,0,*([0]*256)))
fcntl.ioctl(fd,IOC(0,U,1,0),0)
signal.pause()' </dev/null >/tmp/wk-seat.log 2>&1 &
echo $! > /run/wk-bench-seat.pid
sleep 2
kill -0 "$(cat /run/wk-bench-seat.pid)" 2>/dev/null
