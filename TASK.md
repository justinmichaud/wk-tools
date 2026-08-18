Go through this whole project. Take stock.

1) Ensure that every task is organized and properly scoped, and in a reasonable order
2) Look for security gaps or logic gaps. For example, I instructed that the purpose of this repo is to run autonomous agents inside a sandbox, but then when I tried to do a build from inside the sandbox, I was told to give claude access to the host. Look carefully for these kinds of logic gaps.
3) Everything in this repo should be covered by the test plan, and I should be able to run the test plan autonomously at the end of building everything to verify that my setup is working. I should also be able to run it on a new install or after upgrading to ensure no breakage. Every new idea should be covered by the test plan too.
4) Abstraction. Make sure that the target metaphor is abstracted correctly. If bash is not the right tool, switch to python.
5) Scope. Make sure each idea is scoped so it can be handled by one agent.
6) Fragility. Nothing should embed specific details about os versions. All workarounds should be clearly labeled with the version they are needed for, and when they should be fixed
7) Upstreamability. As much as possible should make it upstream, so that I maintain the least amount possible here. Call out places that are likely to break. Match upstream webkit-container-sdk styles and idioms as much as possible.
8) Style. The agents frequently got lost or trapped in loops. Make sure everything is clear and concise, and at the correct level of abstraction. Not too many comments, and everything assumes the reader is fresh and not part of our conversation. Make sure documentation is concise and avoids common llm-isms like "load bearing" or "belt and suspenders". Reduce duplicated code and find good common abstractions.
9) Growth. Make sure this won't silently break one day because update take up more disk space. Check that nothing is growing monotonically, and that there are tools to clean up everything.
10) Discoverability. Make sure the help page is complete but concise.
11) Longevity. Look for places where hardware might wear out (the rpi5 needs the fan to always be on, displays staying on, etc). How will this adapt if I buy new hardware or replace other hardware?
12) Perf. How can we improve perf of builds or lower benchmark noise?
13) Once complete, I will wipe moose and tolken and re-install using the setup script. Will there be any issues or gaps?

Scan https://github.com/justinmichaud/justinmichaud.github.io/wiki/WebKit-JSC-Container-Development-Setup and all the other pages for missing daily tasks that should be automated instead and let me pick.

Delete this file once finished.
