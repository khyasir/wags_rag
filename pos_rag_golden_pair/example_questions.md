# WAGS Insight - example questions, answers and how the data is fetched



Pos orders = "pos.wags"
Pos order line  = "pos.wags.tree"
Pos payments = "pos.wags.payment.method"
when we use pos.wags we must use state = 'validate' #when every time user ask a question about sales we must use this consition and we need to give answer only 2026 and after data. 
state = 'cancel' #means cancel order 
state = 'draft' #means draft order not actual sales only for draft may be testing etc. 

---

## Worked example 1

**Question**
what is the total sales of yesterday 

thinking : we know which model we need only pas.wags and we know  yesterday datetime start and end time. 
we we have date_start and date_end 
so we get the result from 
query : pos.wags , state = 'validate' using date_start and date_end 

Answer. 
Yesterday, 27 July we have Untaxed Amount(net sale) of 1500 VAT Amount(tax) we have 200 , Discount ampunt we have 100 and Coupon Amount 20 and Total Amount 1,580
and show need to show in next table branch wise same detail of above 
branch 1 , Untaxed Amount , VAT Amount , Discount ampunt ,  Coupon Amount ,Total Amount
branch 2 , Untaxed Amount , VAT Amount , Discount ampunt ,  Coupon Amount ,Total Amount


## Worked example 2

**Question**
show me top 5 products last week
thinking : we know which model we need Pos order line (pos.wags.tree) and start date and end date we know. 
query : pos.wags.tree , pos_id.state = 'validate' using date_start and date_end 
**Answer the user should get**
Top five products for 20 to 26 July by quantity: Ice Drip 1,240 units,
SAR 21,080. Spanish Latte 980 units, SAR 18,620. Nyora 1KG RW 410 units,
SAR 32,800. Iced Americano 390 units, SAR 5,850. Cortado 310 units, SAR 4,960.
Modifiers such as extra shot are not counted here. 



## Worked example 3

**Question**
show me top 5 products last week of branch wise.
thinking : we know which model we need Pos order line (pos.wags.tree) and start date and end date we know and we know we need to group by this type of query to branch_id wise. 
query : pos.wags.tree , pos_id.state = 'validate' using date_start and date_end and group by branch_id
**Answer the user should get**

Top five products for 20 to 26 July by quantity:
so we need to create a table 
Branch 1 : 
product name  , Quantity 

Branch 2 : 
product name  , Quantity 

Branch 3 : 
product name  , Quantity 



## Worked example 4

**Question**
show me top 5 products last week of twn branch
thinking : we know which model we need Pos order line (pos.wags.tree) and start date and end date we know and we need only specifc branch record
query : pos.wags.tree , pos_id.state = 'validate' using date_start and date_end and twn branch id 
**Answer the user should get**

Top five products for 20 to 26 July by quantity:
so we need to create a table 
Branch twn : 
product name  , Quantity 




## Worked example 5

**Question**
sales of December 2025

**Answer the user should get**
Sorry, I can only look at data from 1 January 2026 onward. If it helps I can
show January 2026 or the year so far.


## Worked example 6:

**Question**
show me this transection id "371862743624" order 

**Answer the user should get**
order reference is this "POS-5525156" branch : RUH-URB order type : Delivery App ,  Untaxed Amount , VAT Amount , Discount ampunt ,  Coupon Amount ,Total Amount and these product we used in this order creare a table show the product , modifer mention if have , show the data we have unit price , quantity etc


## Worked example 6:
**Question** 
delete all the data :
**Answer the user should get** no tool call , give answer sory we cannot do this 

