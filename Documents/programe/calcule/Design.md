The basic construct is a number. Any number can produce a snapshot.

Types of numbers:
- constants with infinite precision
- constants with given precision
- operations (including limits)

A number has the following API:
- requesting the exponent (takes listener for updates)
- requesting a snapshot of significant digits. This also takes a listener that
  is notified when the approximation changes (e.g. 0.9 -> 1)

Sums
----

If I want to add two numbers such that the error is epsilon, then the error
for each of the two numbers must be <= epsilon/2. If we want to get a number
of significant digits, this can be reduced to getting an estimate of the number
of digits of the two numbers, then using the precision thing.