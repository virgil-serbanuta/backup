Backup algorithm:

* Backups have index + date.
* If my current index is between 2^n and 2^n+1: 
  - Keep: 0 2^n-1 2^n-1+2^n-2 2^n-1+2^n-2+2^n-3 ... 2^n-1+2^n-2+2^n-3+...+1 2^n
  - Then, for indexes between 2^n and index, subtract 2^n and apply the same
    rule.

Example:

```
0
0 1
0 1 2
0 1 2 3
0 1 2 3 4 -> 0 2 3 4
0 1 2 3 4 5 -> 0 2 3 4 5
0 1 2 3 4 5 6 -> 0 2 3 4 5 6
0 1 2 3 4 5 6 7 -> 0 2 3 4 5 6 7
0 1 2 3 4 5 6 7 8 -> 0 4 6 7 8
```
