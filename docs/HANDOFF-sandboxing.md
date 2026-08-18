We should make it so that the permission to push to git can be turned on or off. Ony I should be able to push to git at all, so when I run claude it should be disabled, but work when I run it in the container. A switch is fine.

We should analyse the complete setup and look for sandbox escapes. Newer models are known to be agressive.

Issues I have had:

claude overwriting my work
claude posting on github on my behalf and responding to my reviewers
claude seaching for ssh keys on my host (throught the container sdk)
claude making a suid binary then running it to bypass auto mode
claude searching for a sudo time seat to skip pw auth

Consider which machines can be ssh'd into too.
