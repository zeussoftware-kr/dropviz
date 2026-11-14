import faulthandler

# if a test hangs, print where and stop instead of waiting for the job timeout
faulthandler.dump_traceback_later(600, exit=True)
