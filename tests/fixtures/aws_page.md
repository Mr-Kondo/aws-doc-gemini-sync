

# Example Page
<a name="ExamplePage"></a>

Intro paragraph with `inline_code`, **bold**, and an [internal link](relative-page.html) plus a [markdown-suffixed link](other-page.md).

**Note**  
Quotas apply per Region.

****  

## First section
<a name="first-section"></a>

+ First bullet
+ Second bullet with [absolute link](https://example.com/x)

------
#### [ JSON ]

```
{
  "Version": "2012-10-17",	
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "*"
    }
  ]
}
```

------

## Service quotas
<a name="limits"></a>


| Resource | Default quota | Adjustable | 
| --- | --- | --- | 
| Endpoints per Region | 100 | Yes | 
| Warm pools | 10 | No | 

See [the anchor](#limits) for details.



