export PATH=$PATH:/Users/virgil/runtime-verification/kore/.build/kore/bin:/Users/virgil/.local/bin:/Users/virgil/bin:/Users/virgil/bin/apache-maven-3.5.4/bin/

function promptcmd() {
  RED="\[\033[01;31m\]"
  GREEN="\[\033[01;32m\]"
  BLUE="\[\033[01;34m\]"
  WHITE="\[\033[01;00m\]"
  PS1="$RED`git rev-parse --abbrev-ref HEAD 2> /dev/null | tr -d '\n'`$WHITE:$BLUE\u@\h$WHITE:$GREEN\w$WHITE\$ "
}

HISTORY_FILE=/Users/virgil/.history
export PROMPT_COMMAND='echo $SHELLID $PIPESTATUS $USER@$HOSTNAME $(date +"%Y%m%d %H%M%S") $(printf %q "$PWD") ">>>>" $(history 1) >> $HISTORY_FILE; promptcmd'

alias hs="hs.sh"
alias hs-full="cat /Users/virgil/.history | grep"
alias debug-filter="~/runtime-verification/kore/src/main/python/debugFilter.py"
alias debugger="~/runtime-verification/kore/src/main/python/debugger.py"

[ -f /usr/local/etc/bash_completion ] && . /usr/local/etc/bash_completion

# opam configuration
test -r /Users/virgil/.opam/opam-init/init.sh && . /Users/virgil/.opam/opam-init/init.sh > /dev/null 2> /dev/null || true

# kevm configuration
export PATH=$PATH:/Users/virgil/runtime-verification/klab/bin:/Users/virgil/Library/Python/3.7/bin
export KLAB_EVMS_PATH=/Users/virgil/runtime-verification/klab/evm-semantics
export TMPDIR=/tmp
export PATH=$PATH:/Users/virgil/runtime-verification/k/k-distribution/target/release/k/bin
export PATH="/usr/local/opt/llvm/bin:$PATH"
export CMAKE_PREFIX_PATH=/usr/local/opt/llvm/lib/cmake/llvm/
export PATH=$PATH:/Users/virgil/runtime-verification/llvm-backend/build/install/bin:/Users/virgil/runtime-verification/llvm-backend/build/bin

export LDFLAGS="-L/usr/local/opt/flex/lib $LDFLAGS"
export CPPFLAGS="-I/usr/local/opt/flex/include $CPPFLAGS"

export PATH="/usr/local/opt/flex/bin:$PATH"
export PATH="/Users/virgil/.local/bin:$PATH"
export PATH="/Users/virgil/programe/analiza/depot_tools:$PATH"
export PATH="/usr/local/opt/llvm/bin:$PATH"
export PATH=/usr/local/opt/make/libexec/gnubin:$PATH

export LDFLAGS="-L/usr/local/opt/llvm/lib"
export CPPFLAGS="-I/usr/local/opt/llvm/include"
export JAVA_HOME="/usr/local/opt/openjdk"



# >>> coursier install directory >>>
export PATH="$PATH:/Users/virgil/Library/Application Support/Coursier/bin"
# <<< coursier install directory <<<

export PATH="${HOME}/multiversx-sdk:${PATH}"


export PATH="$HOME/.elan/bin:$PATH"
